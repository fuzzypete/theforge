"""Coordinator notification logic: macOS, ntfy, and human review."""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from . import coord_util as _cu

if TYPE_CHECKING:
    from . import coord_state as _cs
from .config import ForgeConfig
from .review import ReviewResult
from .task import TaskSpec


def _osa_quote(s: str) -> str:
    """Escape and wrap a string for use as an AppleScript string literal."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _notify(title: str, body: str) -> None:
    """Send a native OS notification. Fails silently on unsupported platforms."""
    if shutil.which("osascript") is None:
        return
    script = (
        f"display notification {_osa_quote(body)} "
        f"with title {_osa_quote(title)} "
        f'sound name "default"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def _escalate_notify(
    task: "TaskSpec",
    state: "_cs.CoordinatorState",
    notify: bool,
    config: "ForgeConfig | None" = None,
) -> None:
    """Send an escalation notification if notify is enabled."""
    if not notify:
        return
    _notify(
        f"TheForge: escalated — {task.slug}",
        (state.error or "")[:120],
    )
    if config is None or config.notifications.ntfy is None:
        return
    ntfy = config.notifications.ntfy
    elapsed = 0.0
    if state.started_at:
        try:
            started = datetime.datetime.fromisoformat(state.started_at)
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
        except Exception:
            pass
    last_p1: str | None = None
    if state.review_results:
        p1s = [f for f in state.review_results[-1].findings if f.severity == "P1"]
        if p1s:
            last_p1 = p1s[0].description
    detail = (last_p1 or (state.error or ""))[:120]
    branch = state.branch_name or ""
    first_line = (
        f"{state.review_cycle} cycles exhausted"
        f" — ${state.total_cost:.2f}  {_cu._fmt_duration(elapsed)}"
    )
    body = "\n".join([first_line, detail, f"Branch: {branch}"])
    try:
        _ntfy_publish(
            ntfy.url,
            f"TheForge: \u2717 escalated \u2014 {task.slug}",
            body,
            priority=ntfy.priority,
        )
    except Exception:
        pass


def _ntfy_done_notify(
    task: "TaskSpec",
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    notify: bool,
    summary: str,
    elapsed: float,
    branch_name: str,
) -> None:
    """Publish an ntfy notification when a task reaches DONE. Fails silently."""
    if not notify or config.notifications.ntfy is None:
        return
    ntfy = config.notifications.ntfy
    body = "\n".join(
        [
            f"APPROVE \u2014 ${state.total_cost:.2f}  {_cu._fmt_duration(elapsed)}",
            (summary or "Approved and merged.")[:120],
            f"Branch: {branch_name}",
        ]
    )
    try:
        _ntfy_publish(
            ntfy.url,
            f"TheForge: \u2713 done \u2014 {task.slug}",
            body,
            priority=ntfy.priority,
        )
    except Exception:
        pass


# ── ntfy helpers ──────────────────────────────────────────────────────


def _ntfy_reply_url(base_url: str) -> str:
    """Append '-reply' to the topic segment of the ntfy URL."""
    return base_url.rstrip("/") + "-reply"


def _ntfy_publish(
    url: str,
    title: str,
    body: str,
    priority: str = "high",
    actions: str | None = None,
) -> None:
    """POST a message to an ntfy topic. Fails silently."""
    headers: dict[str, str] = {
        "Title": title,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if actions:
        headers["Actions"] = actions
    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def _ntfy_poll_reply(
    reply_url: str,
    since_ts: int,
    timeout_seconds: int,
) -> tuple[str, str | None]:
    """Poll ntfy reply topic until a valid decision arrives or timeout."""
    deadline = time.monotonic() + timeout_seconds
    poll_url = f"{reply_url}/json?poll=1&since={since_ts}"

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(poll_url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") not in ("message", None):
                    continue
                msg = (obj.get("message") or "").strip()
                msg_lower = msg.lower()
                if msg_lower == "approve":
                    return "approve", None
                if msg_lower == "extend":
                    return "extend", None
                if msg_lower == "escalate":
                    return "escalate", None
                if msg_lower.startswith("reject:"):
                    findings = msg[7:].strip()
                    return "reject", findings or None
        except Exception:
            pass

        sleep_secs = min(10.0, max(0.0, deadline - time.monotonic()))
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    return "timeout", None


def _is_remote_mode(notify: bool, config: "ForgeConfig") -> bool:
    """Return True when all conditions for ntfy-based remote HUMAN_REVIEW are met."""
    return (
        notify and config.notifications.backend == "ntfy" and config.notifications.ntfy is not None
    )


def _remote_human_review(
    state: "_cs.CoordinatorState",
    parsed_review: "ReviewResult",
    workspace_path: "Path",
    branch_name: str,
    task: "TaskSpec",
    config: "ForgeConfig",
    task_start: float,
) -> tuple[str, str | None]:
    """Async ntfy-based human review decision."""
    ntfy = config.notifications.ntfy
    assert ntfy is not None

    reply_url = _ntfy_reply_url(ntfy.url)
    timeout_seconds = config.notifications.human_review_timeout_seconds

    p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    elapsed = time.monotonic() - task_start

    title = f"TheForge: review needed \u2014 {task.slug}"
    body_lines = [
        f"{parsed_review.verdict} ({p1} P1, {p2} P2) \u2014 ${state.total_cost:.2f}  "
        f"{_cu._fmt_duration(elapsed)}",
        parsed_review.summary[:120],
        f"Branch: {branch_name}",
    ]
    body = "\n".join(body_lines)
    actions = (
        f"http, Approve, {reply_url}, method=POST, body=approve; "
        f"http, Extend+1, {reply_url}, method=POST, body=extend; "
        f"http, Escalate, {reply_url}, method=POST, body=escalate"
    )

    _cu._log("─── Remote Human Review (ntfy) ───")
    _cu._log(f"  Topic:   {ntfy.url}")
    _cu._log(f"  Reply:   {reply_url}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    since_ts = int(time.time())
    _ntfy_publish(ntfy.url, title, body, priority=ntfy.priority, actions=actions)

    _poll_start = time.monotonic()
    decision, feedback = _ntfy_poll_reply(reply_url, since_ts, timeout_seconds)
    state.human_review_waited_seconds = time.monotonic() - _poll_start
    state.human_review_mode = "remote"

    if decision == "timeout":
        timeout_title = f"TheForge: timed out waiting for review decision \u2014 {task.slug}"
        _ntfy_publish(
            ntfy.url, timeout_title, "Auto-escalating after timeout.", priority=ntfy.priority
        )
        _cu._log(
            f"Remote review timed out after {_cu._fmt_duration(state.human_review_waited_seconds)}"
        )
    else:
        waited_str = _cu._fmt_duration(state.human_review_waited_seconds or 0)
        _cu._log(f"Remote review decision: {decision!r} (waited {waited_str})")

    return decision, feedback


# ── Human review ─────────────────────────────────────────────────────


def _human_review(
    state: "_cs.CoordinatorState",
    parsed_review: "ReviewResult",
    workspace_path: "Path",
    branch_name: str,
) -> tuple[str, str | None]:
    """Prompt the human operator for a review decision."""

    p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    finding_summary = f"{p1} P1, {p2} P2" if (p1 or p2) else "no findings"

    _cu._log("─── Human Review ───")
    _cu._log(f"  Verdict:   {parsed_review.verdict} ({finding_summary})")
    _cu._log(f"  Summary:   {parsed_review.summary}")
    _cu._log(f"  Workspace: {workspace_path}")
    _cu._log(f"  Branch:    {branch_name}")
    _cu._log(f"  Cost:      ${state.total_cost:.3f}")
    _cu._log("")
    _cu._log("Options:")
    _cu._log("  [a]pprove  → DONE (ready to merge)")
    _cu._log("  [r]eject   → send findings back to dev")
    _cu._log("  [e]scalate → give up")
    _cu._log("")

    state.human_review_mode = "interactive"
    while True:
        print("[forge] Choice [a/r/e]: ", end="", file=sys.stderr, flush=True)
        raw = sys.stdin.readline()
        if not raw:
            _cu._log("EOF on stdin — escalating.")
            return "escalate", None
        choice = raw.strip().lower()
        if choice in ("a", "approve"):
            return "approve", None
        if choice in ("e", "escalate"):
            return "escalate", None
        if choice in ("r", "reject"):
            _cu._log("Enter your findings (empty line to finish):")
            lines: list[str] = []
            while True:
                print("> ", end="", file=sys.stderr, flush=True)
                line = sys.stdin.readline()
                if not line:
                    break
                stripped = line.rstrip("\n")
                if stripped == "":
                    break
                lines.append(stripped)
            return "reject", "\n".join(lines)
        _cu._log("Invalid choice. Enter 'a', 'r', or 'e'.")
