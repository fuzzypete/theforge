"""Coordinator notification logic: macOS, ntfy, and human review."""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from . import coord_util as _cu

if TYPE_CHECKING:
    from . import coord_state as _cs
from .config import ForgeConfig
from .review import ReviewResult
from .task import TaskStory as TaskSpec  # noqa: F401


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
    if config is None:
        return
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
    if config.notifications.ntfy is not None:
        ntfy = config.notifications.ntfy
        try:
            _ntfy_publish(
                ntfy.url,
                f"TheForge: \u2717 escalated \u2014 {task.slug}",
                body,
                priority=ntfy.priority,
            )
        except Exception:
            pass
    if config.notifications.backend not in ("ntfy", "none"):
        from .notify_backends import send_notifications

        send_notifications(
            config,
            f"TheForge: \u2717 escalated \u2014 {task.slug}",
            body,
            is_escalation=True,
        )


def _ntfy_crash_notify(
    task: "TaskSpec",
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    uptime_seconds: float,
) -> None:
    """Publish a notification when a task crashes. Fails silently."""
    phase_name = state.phase.name if state.phase is not None else "UNKNOWN"
    body = "\n".join(
        [
            f"{phase_name} (iter {state.dev_iteration})",
            f"Cost at crash: ${state.total_cost:.2f}",
            f"Uptime: {_cu._fmt_duration(uptime_seconds)}",
        ]
    )
    title = f"TheForge CRASHED \u2014 {task.slug}"
    if config.notifications.ntfy is not None:
        ntfy = config.notifications.ntfy
        try:
            _ntfy_publish(ntfy.url, title, body, priority=ntfy.priority)
        except Exception:
            pass
    if config.notifications.backend not in ("ntfy", "none"):
        from .notify_backends import send_notifications

        send_notifications(config, title, body)


def _ntfy_done_notify(
    task: "TaskSpec",
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    notify: bool,
    summary: str,
    elapsed: float,
    branch_name: str,
) -> None:
    """Publish a notification when a task reaches DONE. Fails silently."""
    if not notify:
        return
    body = "\n".join(
        [
            f"APPROVE \u2014 ${state.total_cost:.2f}  {_cu._fmt_duration(elapsed)}",
            (summary or "Approved and merged.")[:120],
            f"Branch: {branch_name}",
        ]
    )
    title = f"TheForge: \u2713 done \u2014 {task.slug}"
    if config.notifications.ntfy is not None:
        ntfy = config.notifications.ntfy
        try:
            _ntfy_publish(ntfy.url, title, body, priority=ntfy.priority)
        except Exception:
            pass
    if config.notifications.backend not in ("ntfy", "none"):
        from .notify_backends import send_notifications

        send_notifications(config, title, body)


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
    # HTTP header values must be ASCII-safe for urllib/http.client.
    safe_title = (
        title.replace("✓", "OK")
        .replace("✗", "X")
        .replace("—", "-")
        .replace("–", "-")
        .replace("…", "...")
    )
    safe_title = (
        unicodedata.normalize("NFKD", safe_title).encode("ascii", "ignore").decode("ascii")
    )
    headers: dict[str, str] = {
        "Title": safe_title,
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
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as exc:
        _cu._log(f"WARNING: ntfy publish failed (continuing): {exc}")


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


def _ntfy_poll_plan_reply(
    reply_url: str,
    since_ts: int,
    timeout_seconds: int,
) -> str:
    """Poll ntfy reply topic for a plan review decision.

    Returns 'approve', 'regenerate', 'abandon', or 'timeout'.
    """
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
                msg = (obj.get("message") or "").strip().lower()
                if msg == "approve":
                    return "approve"
                if msg == "regenerate":
                    return "regenerate"
                if msg == "abandon":
                    return "abandon"
        except Exception:
            pass

        sleep_secs = min(10.0, max(0.0, deadline - time.monotonic()))
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    return "timeout"


_BLOCKING_POLL_CHUNK = 300  # seconds per poll iteration in blocking mode


# ── Escalate gate ─────────────────────────────────────────────────────


def _ntfy_poll_escalate_reply(
    reply_url: str,
    since_ts: int,
    timeout_seconds: int,
) -> str:
    """Poll ntfy reply topic for an escalate gate decision.

    Returns 'approve', 'reject', 'continue', or 'timeout'.
    """
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
                msg = (obj.get("message") or "").strip().lower()
                if msg == "approve":
                    return "approve"
                if msg == "reject":
                    return "reject"
                if msg == "continue":
                    return "continue"
        except Exception:
            pass

        sleep_secs = min(10.0, max(0.0, deadline - time.monotonic()))
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    return "timeout"


def _escalate_gate_interactive(
    state: "_cs.CoordinatorState",
    escalate_reason: str,
    reviewer_verdicts: dict[str, str],
    gate_result: str | None,
) -> str:
    """Interactive escalate gate prompt. Returns 'approve' | 'reject' | 'continue'."""
    _cu._log("─── ESCALATE Gate ───")
    _cu._log(f"  Reason:   {escalate_reason}")
    if reviewer_verdicts:
        verdicts_str = "  ".join(f"{k}({v})" for k, v in reviewer_verdicts.items())
        _cu._log(f"  Reviewers: {verdicts_str}")
    if gate_result:
        _cu._log(f"  Gate:     {gate_result}")
    _cu._log(f"  Cost:     ${state.total_cost:.3f}")
    _cu._log(f"  Dev iter: {state.dev_iteration}  Review cycles: {state.review_cycle}")
    _cu._log("")
    _cu._log("  Choose:")
    _cu._log("    [a] Approve  — treat as APPROVE, create PR / merge")
    _cu._log("    [r] Reject   — exit as ESCALATE, preserve worktree")
    _cu._log("    [c] Continue — run one more review cycle")

    while True:
        print("[forge] Choice [a/r/c]: ", end="", file=sys.stderr, flush=True)
        raw = sys.stdin.readline()
        if not raw:
            _cu._log("EOF on stdin — rejecting.")
            return "reject"
        choice = raw.strip().lower()
        if choice in ("a", "approve"):
            return "approve"
        if choice in ("r", "reject"):
            return "reject"
        if choice in ("c", "continue"):
            return "continue"
        _cu._log("Invalid choice. Enter 'a', 'r', or 'c'.")


def _escalate_gate_remote(
    state: "_cs.CoordinatorState",
    task: "TaskSpec",
    config: "ForgeConfig",
    escalate_reason: str,
    reviewer_verdicts: dict[str, str],
    gate_result: str | None,
) -> str:
    """Ntfy-backed escalate gate. Returns 'approve' | 'reject' | 'continue'."""
    ntfy = config.notifications.ntfy
    assert ntfy is not None

    reply_url = _ntfy_reply_url(ntfy.url)
    timeout_seconds = config.notifications.human_review_timeout_seconds

    approve_count = sum(1 for v in reviewer_verdicts.values() if v == "APPROVE")
    total_count = len(reviewer_verdicts)
    verdict_line = (
        f"{approve_count}/{total_count} reviewers APPROVE" if reviewer_verdicts else "no verdicts"
    )
    gate_line = gate_result or ""
    body = "\n".join(filter(None, [verdict_line, gate_line, escalate_reason[:120]]))

    actions = (
        f"http, Approve, {reply_url}, method=POST, body=approve; "
        f"http, Reject, {reply_url}, method=POST, body=reject; "
        f"http, Continue, {reply_url}, method=POST, body=continue"
    )

    _cu._log("─── Remote Escalate Gate (ntfy) ───")
    _cu._log(f"  Topic:   {ntfy.url}")
    _cu._log(f"  Reply:   {reply_url}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    since_ts = int(time.time())
    title = f"TheForge: ESCALATE \u2014 {task.slug}"
    _ntfy_publish(ntfy.url, title, body, priority=ntfy.priority, actions=actions)

    _poll_start = time.monotonic()
    # Poll with mandatory timeout — never block indefinitely
    while True:
        decision = _ntfy_poll_escalate_reply(reply_url, since_ts, _BLOCKING_POLL_CHUNK)
        if decision != "timeout":
            break
        waited_so_far = time.monotonic() - _poll_start
        if timeout_seconds > 0 and waited_so_far >= timeout_seconds:
            _cu._log(
                f"  ESCALATE gate timed out after {_cu._fmt_duration(waited_so_far)}"
                " — auto-escalating"
            )
            decision = "timeout"
            break
        elapsed = _cu._fmt_duration(waited_so_far)
        _cu._log(f"  ESCALATE gate still waiting for decision (elapsed {elapsed})")

    waited = time.monotonic() - _poll_start
    state.human_review_waited_seconds = (state.human_review_waited_seconds or 0.0) + waited
    waited_str = _cu._fmt_duration(waited)
    _cu._log(f"  Escalate gate decision: {decision!r} (waited {waited_str})")
    return decision


def _plan_review_remote(
    state: "_cs.CoordinatorState",
    plan_text: str,
    workspace_path: "Path",
    task: "TaskSpec",
    config: "ForgeConfig",
) -> str:
    """Ntfy-backed remote plan review. Returns 'approve' | 'regenerate' | 'abandon'."""
    ntfy = config.notifications.ntfy
    assert ntfy is not None

    reply_url = _ntfy_reply_url(ntfy.url)
    timeout_seconds = config.plan_review.timeout_seconds

    # Build notification body: first 3 lines of the plan, truncated to 200 chars
    first_3_lines = "\n".join(plan_text.splitlines()[:3])
    plan_summary = first_3_lines[:200]
    if len(first_3_lines) > 200:
        plan_summary += "\u2026"
    body = f"{plan_summary}\nWorktree: .forge/worktrees/{task.slug}"

    actions = (
        f"http, Approve, {reply_url}, method=POST, body=approve; "
        f"http, Regenerate, {reply_url}, method=POST, body=regenerate; "
        f"http, Abandon, {reply_url}, method=POST, body=abandon"
    )

    _cu._log("─── Remote Plan Review (ntfy) ───")
    _cu._log(f"  Topic:   {ntfy.url}")
    _cu._log(f"  Reply:   {reply_url}")
    mode = config.plan_review.mode
    if mode == "advisory":
        _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)} (advisory)")
    else:
        _cu._log("  Timeout: none (blocking — polls until explicit decision)")

    since_ts = int(time.time())
    title = f"TheForge: plan ready \u2014 {task.slug}"
    _ntfy_publish(ntfy.url, title, body, priority=ntfy.priority, actions=actions)

    _pr_start = time.monotonic()
    state.plan_review_mode = "remote"

    if mode == "advisory":
        decision = _ntfy_poll_plan_reply(reply_url, since_ts, timeout_seconds)
        state.plan_review_waited_seconds = time.monotonic() - _pr_start
        if decision == "timeout":
            waited_str = _cu._fmt_duration(state.plan_review_waited_seconds or 0)
            _cu._log(f"  ⚠ PLAN_REVIEW   advisory timeout — auto-approving after {waited_str}")
            _ntfy_publish(
                ntfy.url,
                f"TheForge: plan auto-approved \u2014 {task.slug}",
                "Advisory timeout reached — proceeding to DEV.",
                priority=ntfy.priority,
            )
            state.plan_review_mode = "advisory-timeout"
            return "approve"
    else:
        # Blocking: poll with mandatory timeout
        while True:
            decision = _ntfy_poll_plan_reply(reply_url, since_ts, _BLOCKING_POLL_CHUNK)
            if decision != "timeout":
                break
            waited_so_far = time.monotonic() - _pr_start
            if timeout_seconds > 0 and waited_so_far >= timeout_seconds:
                _cu._log(
                    f"  PLAN_REVIEW gate timed out after {_cu._fmt_duration(waited_so_far)}"
                    " — auto-approving"
                )
                decision = "approve"
                break
            elapsed = _cu._fmt_duration(waited_so_far)
            _cu._log(f"  PLAN_REVIEW   still waiting for decision (elapsed {elapsed})")
        state.plan_review_waited_seconds = time.monotonic() - _pr_start

    waited_str = _cu._fmt_duration(state.plan_review_waited_seconds or 0)
    _cu._log(f"  Remote plan review decision: {decision!r} (waited {waited_str})")
    return decision


def _is_remote_mode(notify: bool, config: "ForgeConfig") -> bool:
    """Return True when all conditions for ntfy-based remote HUMAN_REVIEW are met."""
    return (
        notify and config.notifications.backend == "ntfy" and config.notifications.ntfy is not None
    )


def _is_pending_file_mode(notify: bool, config: "ForgeConfig") -> bool:
    """Return True when the coordinator should use the pending-file interface for HITL.

    This activates whenever notify is enabled and at least one backend is configured,
    regardless of which backends. The pending file is the decision channel; backends
    are only notification transports.
    """
    return notify and len(config.notifications.backends) > 0


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


def _plan_review_interactive(
    state: "_cs.CoordinatorState",
    plan_text: str,
    workspace_path: "Path",
    task: "TaskSpec",
) -> str:
    """Interactive plan review. Returns 'approve' | 'regenerate' | 'abandon'."""

    del state, task

    print(plan_text, end="" if plan_text.endswith("\n") else "\n", file=sys.stdout, flush=True)
    print(f"Plan at: {workspace_path / 'forge_plan.md'}", file=sys.stderr, flush=True)

    while True:
        print("Plan ready. Review forge_plan.md and choose:", file=sys.stderr, flush=True)
        print("  [a] Approve   — proceed to DEV", file=sys.stderr, flush=True)
        print(
            "  [e] Edit      — edit forge_plan.md externally, then re-enter 'a'",
            file=sys.stderr,
            flush=True,
        )
        print(
            "  [r] Regenerate — discard and re-run PLAN agent (once)",
            file=sys.stderr,
            flush=True,
        )
        print(
            "  [x] Abandon   — cancel run, leave worktree intact",
            file=sys.stderr,
            flush=True,
        )
        print("Choice [a/e/r/x]: ", end="", file=sys.stderr, flush=True)

        raw = sys.stdin.readline()
        if not raw:
            return "abandon"

        choice = raw.strip().lower()
        if choice in ("a", "approve"):
            return "approve"
        if choice in ("e", "edit"):
            print("Edit the plan, then enter 'a' to approve:", file=sys.stderr, flush=True)
            continue
        if choice in ("r", "regenerate"):
            return "regenerate"
        if choice in ("x", "abandon"):
            return "abandon"

        print("Invalid choice. Enter 'a', 'e', 'r', or 'x'.", file=sys.stderr, flush=True)


# ── Pending-file HITL gates ───────────────────────────────────────────


def _pending_human_review(
    state: "_cs.CoordinatorState",
    parsed_review: "ReviewResult",
    workspace_path: "Path",
    branch_name: str,
    task: "TaskSpec",
    config: "ForgeConfig",
    task_start: float,
    run_id: str = "",
) -> tuple[str, str | None]:
    """Pending-file-based human review decision.

    Writes a pending file, sends notifications, polls for decision.
    Returns (decision, feedback) where decision is one of:
    approve | reject | escalate | extend | timeout
    """
    from . import pending as _pending
    from .notify_backends import send_notifications

    p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    elapsed = time.monotonic() - task_start
    timeout_seconds = config.notifications.human_review_timeout_seconds

    reason = (
        f"{parsed_review.verdict} ({p1} P1, {p2} P2) — ${state.total_cost:.2f} "
        f"{_cu._fmt_duration(elapsed)}\n{parsed_review.summary[:120]}\nBranch: {branch_name}"
    )

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Pending Human Review ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="HUMAN_REVIEW",
        reason=reason,
        options=["approve", "reject", "escalate", "extend"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    send_notifications(
        config,
        title=f"TheForge: review needed — {task.slug}",
        body=reason[:300],
    )

    _poll_start = time.monotonic()
    decision, _decided_at = _pending.poll_pending(
        _eff_run_id, timeout_seconds, project_root=project_root
    )
    state.human_review_waited_seconds = time.monotonic() - _poll_start
    state.human_review_mode = "pending"

    _pending.cleanup_pending(_eff_run_id, project_root)

    if decision == "timeout":
        _cu._log(
            f"Pending review timed out after"
            f" {_cu._fmt_duration(state.human_review_waited_seconds)}"
        )
        return "timeout", None

    waited_str = _cu._fmt_duration(state.human_review_waited_seconds or 0)
    _cu._log(f"Pending review decision: {decision!r} (waited {waited_str})")
    return decision, None


def _pending_escalate_gate(
    state: "_cs.CoordinatorState",
    task: "TaskSpec",
    config: "ForgeConfig",
    escalate_reason: str,
    reviewer_verdicts: dict[str, str],
    gate_result: str | None,
    run_id: str = "",
) -> str:
    """Pending-file-based escalate gate. Returns 'approve' | 'reject' | 'continue'."""
    from . import pending as _pending
    from .notify_backends import send_notifications

    timeout_seconds = config.notifications.human_review_timeout_seconds
    approve_count = sum(1 for v in reviewer_verdicts.values() if v == "APPROVE")
    total_count = len(reviewer_verdicts)
    verdict_line = (
        f"{approve_count}/{total_count} reviewers APPROVE" if reviewer_verdicts else "no verdicts"
    )
    gate_line = gate_result or ""
    reason = "\n".join(filter(None, [verdict_line, gate_line, escalate_reason[:120]]))

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Pending Escalate Gate ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="ESCALATE",
        reason=reason,
        options=["approve", "reject", "continue"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    send_notifications(
        config,
        title=f"TheForge: ESCALATE — {task.slug}",
        body=reason[:300],
    )

    _poll_start = time.monotonic()
    decision, _decided_at = _pending.poll_pending(
        _eff_run_id, timeout_seconds, project_root=project_root
    )
    waited = time.monotonic() - _poll_start
    state.human_review_waited_seconds = (state.human_review_waited_seconds or 0.0) + waited

    _pending.cleanup_pending(_eff_run_id, project_root)

    waited_str = _cu._fmt_duration(waited)
    if decision == "timeout":
        _cu._log(f"  Escalate gate timed out after {waited_str} — auto-rejecting")
        return "reject"

    _cu._log(f"  Escalate gate decision: {decision!r} (waited {waited_str})")
    if decision in ("approve", "reject", "continue"):
        return decision
    return "reject"


def _pending_plan_review(
    state: "_cs.CoordinatorState",
    plan_text: str,
    workspace_path: "Path",
    task: "TaskSpec",
    config: "ForgeConfig",
    run_id: str = "",
) -> str:
    """Pending-file-based plan review. Returns 'approve' | 'regenerate' | 'abandon'."""
    from . import pending as _pending
    from .notify_backends import send_notifications

    timeout_seconds = config.plan_review.timeout_seconds
    first_3_lines = "\n".join(plan_text.splitlines()[:3])
    plan_summary = first_3_lines[:200]
    reason = f"{plan_summary}\nWorktree: .forge/worktrees/{task.slug}"

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Pending Plan Review ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="PLAN_REVIEW",
        reason=reason,
        options=["approve", "regenerate", "abandon"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    send_notifications(
        config,
        title=f"TheForge: plan ready — {task.slug}",
        body=reason[:300],
    )

    _pr_start = time.monotonic()
    state.plan_review_mode = "pending"
    mode = config.plan_review.mode

    decision, _decided_at = _pending.poll_pending(
        _eff_run_id, timeout_seconds, project_root=project_root
    )
    state.plan_review_waited_seconds = time.monotonic() - _pr_start

    _pending.cleanup_pending(_eff_run_id, project_root)

    if decision == "timeout":
        waited_str = _cu._fmt_duration(state.plan_review_waited_seconds or 0)
        if mode == "advisory":
            _cu._log(f"  ⚠ PLAN_REVIEW   advisory timeout — auto-approving after {waited_str}")
            state.plan_review_mode = "advisory-timeout"
            return "approve"
        else:
            _cu._log(f"  ✗ PLAN_REVIEW   blocking timeout after {waited_str} — abandoning")
            return "abandon"

    waited_str = _cu._fmt_duration(state.plan_review_waited_seconds or 0)
    _cu._log(f"  Pending plan review decision: {decision!r} (waited {waited_str})")
    if decision in ("approve", "regenerate", "abandon"):
        return decision
    return "abandon"
