"""Remote (ntfy-backed) HITL gate implementations."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import util as _cu
from .ntfy_client import (
    _BLOCKING_POLL_CHUNK,
    _ntfy_poll_escalate_reply,
    _ntfy_poll_plan_reply,
    _ntfy_poll_reply,
    _ntfy_publish,
    _ntfy_reply_url,
)

if TYPE_CHECKING:
    from theforge.config import ForgeConfig
    from theforge.review import ReviewResult
    from theforge.task import TaskStory

    from . import state as _cs


def _escalate_gate_remote(
    state: "_cs.CoordinatorState",
    task: "TaskStory",
    config: "ForgeConfig",
    escalate_reason: str,
    reviewer_verdicts: dict[str, str],
    gate_result: "str | None",
) -> str:
    """Ntfy-backed escalate gate. Returns 'approve' | 'reject' | 'continue'.

    The Approve action button is published only when an approvable reviewer
    result is retained — a notification must not offer a tap the run would have
    to substitute away from (#2300).
    """
    from .escalate_actions import (  # noqa: PLC0415
        ACCEPT_UNAVAILABLE_REASON,
        approvable_review_result,
    )

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
    can_approve = approvable_review_result(state) is not None
    not_offered_line = "" if can_approve else f"Approve not offered: {ACCEPT_UNAVAILABLE_REASON}"
    body = "\n".join(
        filter(None, [verdict_line, gate_line, escalate_reason[:120], not_offered_line])
    )

    _action_specs = []
    if can_approve:
        _action_specs.append(f"http, Approve, {reply_url}, method=POST, body=approve")
    _action_specs.append(f"http, Reject, {reply_url}, method=POST, body=reject")
    _action_specs.append(f"http, Continue, {reply_url}, method=POST, body=continue")
    actions = "; ".join(_action_specs)

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
                " — no decision received; preserving for an operator decision"
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
    task: "TaskStory",
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


def _remote_human_review(
    state: "_cs.CoordinatorState",
    parsed_review: "ReviewResult",
    workspace_path: "Path",
    branch_name: str,
    task: "TaskStory",
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
        f"{parsed_review.verdict} ({p1} P1, {p2} P2) \u2014 "
        f"{_cu._fmt_cost_total(state.total_cost_measured, state.total_cost)}  "
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
