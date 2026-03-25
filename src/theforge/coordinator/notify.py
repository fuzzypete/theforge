"""Coordinator notification logic: macOS, ntfy, and human review."""

from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from theforge.artifacts import PLAN_PATH
from theforge.config import ForgeConfig
from theforge.review import ReviewResult
from theforge.task import TaskStory as TaskSpec  # noqa: F401

from . import util as _cu
from .ntfy_client import _ntfy_publish

if TYPE_CHECKING:
    from . import state as _cs


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
        from theforge.notify_backends import send_notifications

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
        from theforge.notify_backends import send_notifications

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
        from theforge.notify_backends import send_notifications

        send_notifications(config, title, body)


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
    print(f"Plan at: {workspace_path / PLAN_PATH}", file=sys.stderr, flush=True)

    while True:
        print("Plan ready. Review .forge/plan.md and choose:", file=sys.stderr, flush=True)
        print("  [a] Approve   — proceed to DEV", file=sys.stderr, flush=True)
        print(
            "  [e] Edit      — edit .forge/plan.md externally, then re-enter 'a'",
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


def _escalate_gate_interactive(
    state: "_cs.CoordinatorState",
    escalate_reason: str,
    reviewer_verdicts: dict[str, str],
    gate_result: "str | None",
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
