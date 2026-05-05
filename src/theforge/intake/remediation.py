"""Sprint-intake remediation orchestrator.

Runs once per sprint at the runner injection point. For every task with a
GitHub issue, fetches the current body+labels, runs the grooming check,
applies any mechanical patches, and — when ``intake.auto_fix: true`` —
hands semantic findings to a one-pass agent rewrite. The gate is rerun at
most once per story; no retry loop.

This module owns GitHub I/O via injectable callables so the orchestration
remains testable without network. Production wires real ``gh`` invocations.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..shape_check import check as shape_check
from ..task import TaskStory
from .agent_rewrite import AgentRewriteResult
from .findings import FixType, IntakeFinding, IntakeSeverity, findings_from_shape_result
from .groom import groom_check
from .mechanical import apply_mechanical_fixes

_log = logging.getLogger("theforge.intake")


class IntakeOutcomeKind(str, Enum):
    """Per-story outcome of the intake remediation pass."""

    PASSED = "passed"  # no blocking findings, nothing to do
    REMEDIATED = "remediated"  # auto-fix applied, story stays in sprint
    DROPPED_SHAPE = "dropped_shape"  # gate failed, no auto-fix
    DROPPED_AFTER_FIX = "dropped_after_fix"  # auto-fix attempted but did not resolve


@dataclass(frozen=True)
class IntakeOutcome:
    """Per-story result of the intake remediation pass."""

    slug: str
    kind: IntakeOutcomeKind
    findings: tuple[IntakeFinding, ...] = field(default_factory=tuple)
    proposed_replacement: str | None = None
    detail: str = ""
    audit: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "kind": self.kind.value,
            "findings": [f.as_dict() for f in self.findings],
            "proposed_replacement": self.proposed_replacement,
            "detail": self.detail,
            "audit": dict(self.audit),
        }


# Type aliases for the injected I/O seams.
FetchIssueDetail = Callable[[int, Path | None], dict | None]
PostIssueComment = Callable[[int, str, Path | None], bool]
EditIssueBody = Callable[[int, str, Path | None], bool]
AgentRewriteCaller = Callable[[str, list[IntakeFinding], list[str]], AgentRewriteResult]


def _default_fetch_detail(number: int, project_root: Path | None) -> dict | None:
    """Fetch ``{title, body, labels}`` for an issue via ``gh``.

    Returns ``None`` on any fetch failure so callers can fail-open.
    """
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", str(number), "--json", "title,body,labels,comments"],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    label_names = [
        lbl.get("name", "")
        for lbl in (data.get("labels") or [])
        if isinstance(lbl, dict) and lbl.get("name")
    ]
    comment_bodies = [
        (c.get("body") or "")
        for c in (data.get("comments") or [])
        if isinstance(c, dict) and (c.get("body") or "").strip()
    ]
    return {
        "title": data.get("title", "") or "",
        "body": data.get("body", "") or "",
        "labels": label_names,
        "comments": comment_bodies,
    }


def _default_post_comment(number: int, body: str, project_root: Path | None) -> bool:
    try:
        proc = subprocess.run(
            ["gh", "issue", "comment", str(number), "--body", body],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.warning("gh issue comment #%s failed: %s", number, exc)
        return False
    if proc.returncode != 0:
        _log.warning(
            "gh issue comment #%s failed (exit %d): %s",
            number,
            proc.returncode,
            (proc.stderr or proc.stdout).strip(),
        )
        return False
    return True


def _default_edit_body(number: int, body: str, project_root: Path | None) -> bool:
    try:
        proc = subprocess.run(
            ["gh", "issue", "edit", str(number), "--body", body],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.warning("gh issue edit #%s failed: %s", number, exc)
        return False
    if proc.returncode != 0:
        _log.warning(
            "gh issue edit #%s failed (exit %d): %s",
            number,
            proc.returncode,
            (proc.stderr or proc.stdout).strip(),
        )
        return False
    return True


def _blocking(findings: list[IntakeFinding]) -> list[IntakeFinding]:
    return [f for f in findings if f.severity is IntakeSeverity.BLOCK]


def _format_remediation_comment(
    findings: list[IntakeFinding],
    replacement: str | None,
    *,
    header: str = "**TheForge intake remediation — proposed replacement**",
    findings_label: str = "Findings:",
    replacement_label: str = "Proposed replacement issue body:",
) -> str:
    lines = [header, ""]
    lines.append(findings_label)
    for f in findings:
        lines.append(f"- `{f.code}` ({f.location}): {f.problem}")
    if replacement:
        lines.append("")
        lines.append(replacement_label)
        lines.append("")
        lines.append("```markdown")
        lines.append(replacement)
        lines.append("```")
    return "\n".join(lines)


def _gather_findings(
    title: str,
    body: str,
    labels: list[str],
    *,
    grooming_enabled: bool,
) -> list[IntakeFinding]:
    """Run shape check + (optional) grooming and return combined findings."""
    shape_result = shape_check(title, body, labels)
    findings = findings_from_shape_result(shape_result)
    if grooming_enabled:
        findings.extend(groom_check(title, body, labels))
    return findings


def _build_audit(
    *,
    consumed: list[IntakeFinding],
    semantic_remaining: list[IntakeFinding],
    agent: AgentRewriteResult | None = None,
    remediation_source: str = "none",
    issue_updated: bool = False,
    comment_posted: bool = False,
) -> dict[str, object]:
    return {
        "remediation_source": remediation_source,
        "mechanical_findings": [f.as_dict() for f in consumed],
        "semantic_findings": [f.as_dict() for f in semantic_remaining],
        "agent": {
            "attempted": bool(agent.attempted) if agent is not None else False,
            "detail": agent.detail if agent is not None else "",
            "profile_name": agent.profile_name if agent is not None else None,
            "model_used": agent.model_used if agent is not None else None,
            "cost_usd": agent.cost_usd if agent is not None else None,
            "transport_used": agent.transport_used if agent is not None else None,
        },
        "issue_updated": issue_updated,
        "comment_posted": comment_posted,
    }


def _remediate_one(
    *,
    slug: str,
    issue_number: int,
    title: str,
    body: str,
    labels: list[str],
    comments: list[str],
    grooming_enabled: bool,
    auto_fix_enabled: bool,
    auto_fix_mode: str,
    agent_caller: AgentRewriteCaller | None,
    missing_agent_detail: str,
    post_comment: PostIssueComment,
    edit_body: EditIssueBody,
    project_root: Path | None,
) -> IntakeOutcome:
    """Run the full intake/auto-fix sequence for a single story."""
    findings = _gather_findings(title, body, labels, grooming_enabled=grooming_enabled)
    blocking = _blocking(findings)
    if not blocking:
        return IntakeOutcome(
            slug=slug,
            kind=IntakeOutcomeKind.PASSED,
            findings=tuple(findings),
            audit=_build_audit(consumed=[], semantic_remaining=[]),
        )

    # Apply mechanical patches first; they need no LLM and never fail.
    patched_body, patched_labels, _consumed, remaining = apply_mechanical_fixes(
        body, labels, blocking
    )
    semantic_remaining = [f for f in remaining if f.fix_type is FixType.SEMANTIC]

    if not auto_fix_enabled:
        # Auto-fix disabled — story is dropped; no agent pass attempted.
        return IntakeOutcome(
            slug=slug,
            kind=IntakeOutcomeKind.DROPPED_SHAPE,
            findings=tuple(blocking),
            detail="auto-fix disabled",
            audit=_build_audit(consumed=[], semantic_remaining=blocking),
        )

    # Even if mechanical patches resolved everything, the gate must rerun once
    # to verify. If no semantic findings remain, skip the agent call.
    proposed_body = patched_body
    agent_result: AgentRewriteResult | None = None
    if semantic_remaining:
        if agent_caller is None:
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_SHAPE,
                findings=tuple(blocking),
                detail=missing_agent_detail,
                audit=_build_audit(
                    consumed=_consumed,
                    semantic_remaining=semantic_remaining,
                    remediation_source="missing_agent",
                ),
            )
        agent_result = agent_caller(patched_body, semantic_remaining, comments)
        if not agent_result.replacement:
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
                findings=tuple(blocking),
                proposed_replacement=None,
                detail=agent_result.detail or "agent remediation produced no output",
                audit=_build_audit(
                    consumed=_consumed,
                    semantic_remaining=semantic_remaining,
                    agent=agent_result,
                    remediation_source="agent_no_output",
                ),
            )
        proposed_body = agent_result.replacement

    # Re-run shape + grooming once on the proposed body to verify resolution.
    rerun_findings = _gather_findings(
        title, proposed_body, patched_labels, grooming_enabled=grooming_enabled
    )
    rerun_blocking = _blocking(rerun_findings)

    if auto_fix_mode == "edit":
        if rerun_blocking:
            # Don't edit a body that still fails — but do not silently discard
            # the candidate either. Post the proposed replacement plus the
            # remaining blocking findings as a comment so the operator can
            # continue from the candidate instead of redoing the rewrite.
            salvage_comment = _format_remediation_comment(
                rerun_blocking,
                proposed_body,
                header=("**TheForge intake remediation — candidate failed rerun gate**"),
                findings_label=("Remaining blocking findings against the candidate body:"),
                replacement_label=(
                    "Candidate replacement body "
                    "(rerun gate still failing — operator review required):"
                ),
            )
            posted = post_comment(issue_number, salvage_comment, project_root)
            detail = (
                "rerun gate still failing; candidate posted as issue comment for operator review"
                if posted
                else "rerun gate still failing; failed to post candidate comment"
            )
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
                findings=tuple(rerun_blocking),
                proposed_replacement=proposed_body,
                detail=detail,
                audit=_build_audit(
                    consumed=_consumed,
                    semantic_remaining=semantic_remaining,
                    agent=agent_result,
                    remediation_source=("agent" if agent_result is not None else "mechanical"),
                    comment_posted=posted,
                ),
            )
        if not edit_body(issue_number, proposed_body, project_root):
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
                findings=tuple(blocking),
                proposed_replacement=proposed_body,
                detail="gh issue edit failed",
                audit=_build_audit(
                    consumed=_consumed,
                    semantic_remaining=semantic_remaining,
                    agent=agent_result,
                    remediation_source=("agent" if agent_result is not None else "mechanical"),
                ),
            )
        return IntakeOutcome(
            slug=slug,
            kind=IntakeOutcomeKind.REMEDIATED,
            findings=tuple(blocking),
            proposed_replacement=proposed_body,
            detail="edit mode: issue body updated, gate rerun passed",
            audit=_build_audit(
                consumed=_consumed,
                semantic_remaining=semantic_remaining,
                agent=agent_result,
                remediation_source=("agent" if agent_result is not None else "mechanical"),
                issue_updated=True,
            ),
        )

    # Default: comment mode. Post the proposed replacement and drop the story
    # from this sprint. The contract is unchanged on disk; the operator
    # decides whether to accept the rewrite.
    comment = _format_remediation_comment(blocking, proposed_body)
    posted = post_comment(issue_number, comment, project_root)
    if rerun_blocking:
        return IntakeOutcome(
            slug=slug,
            kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
            findings=tuple(rerun_blocking),
            proposed_replacement=proposed_body,
            detail="comment mode: posted replacement; rerun gate still failing",
            audit=_build_audit(
                consumed=_consumed,
                semantic_remaining=semantic_remaining,
                agent=agent_result,
                remediation_source=("agent" if agent_result is not None else "mechanical"),
                comment_posted=posted,
            ),
        )
    return IntakeOutcome(
        slug=slug,
        kind=IntakeOutcomeKind.DROPPED_SHAPE,
        findings=tuple(blocking),
        proposed_replacement=proposed_body,
        detail="comment mode: proposed replacement posted; story dropped",
        audit=_build_audit(
            consumed=_consumed,
            semantic_remaining=semantic_remaining,
            agent=agent_result,
            remediation_source=("agent" if agent_result is not None else "mechanical"),
            comment_posted=posted,
        ),
    )


def run_intake_remediation(
    tasks: list[TaskStory],
    project_root: Path | None,
    *,
    grooming_enabled: bool = False,
    auto_fix_enabled: bool = False,
    auto_fix_mode: str = "comment",
    agent_caller: AgentRewriteCaller | None = None,
    missing_agent_detail: str = "auto-fix enabled but no agent caller wired",
    fetch_detail: FetchIssueDetail = _default_fetch_detail,
    post_comment: PostIssueComment = _default_post_comment,
    edit_body: EditIssueBody = _default_edit_body,
) -> dict[str, IntakeOutcome]:
    """Run the intake remediation pass over the full normalized task list.

    Returns a slug → ``IntakeOutcome`` map covering every task. Tasks without
    a linked GitHub issue are returned as ``PASSED`` with empty findings —
    the grooming check is GH-only because it needs the live issue body.

    When both ``grooming_enabled`` and ``auto_fix_enabled`` are False, this
    is a pure no-op: every task is returned as ``PASSED`` with empty
    findings, no shape check is run here, and no GH calls are made. (The
    shape gate already filtered upstream in ``cli/sprint.py``.)
    """
    outcomes: dict[str, IntakeOutcome] = {}

    for task in tasks:
        slug = task.slug
        # File-based stories or unlinked tasks: nothing to do.
        if task.github_issue is None:
            outcomes[slug] = IntakeOutcome(slug=slug, kind=IntakeOutcomeKind.PASSED)
            continue

        if not grooming_enabled and not auto_fix_enabled:
            # Backward-compat path: behave identically to today.
            outcomes[slug] = IntakeOutcome(slug=slug, kind=IntakeOutcomeKind.PASSED)
            continue

        detail = fetch_detail(task.github_issue, project_root)
        if detail is None:
            # Fail-open: leave story runnable; downstream phases will surface
            # any real fetch error.
            outcomes[slug] = IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.PASSED,
                detail="issue detail fetch failed; fail-open",
            )
            continue

        outcomes[slug] = _remediate_one(
            slug=slug,
            issue_number=task.github_issue,
            title=detail.get("title", "") or "",
            body=detail.get("body", "") or "",
            labels=list(detail.get("labels", []) or []),
            comments=list(detail.get("comments", []) or []),
            grooming_enabled=grooming_enabled,
            auto_fix_enabled=auto_fix_enabled,
            auto_fix_mode=auto_fix_mode,
            agent_caller=agent_caller,
            missing_agent_detail=missing_agent_detail,
            post_comment=post_comment,
            edit_body=edit_body,
            project_root=project_root,
        )

    return outcomes
