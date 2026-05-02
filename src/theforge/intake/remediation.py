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

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "kind": self.kind.value,
            "findings": [f.as_dict() for f in self.findings],
            "proposed_replacement": self.proposed_replacement,
            "detail": self.detail,
        }


# Type aliases for the injected I/O seams.
FetchIssueDetail = Callable[[int, Path | None], dict | None]
PostIssueComment = Callable[[int, str, Path | None], bool]
EditIssueBody = Callable[[int, str, Path | None], bool]
AgentRewriteCaller = Callable[[str, list[IntakeFinding]], str | None]


def _default_fetch_detail(number: int, project_root: Path | None) -> dict | None:
    """Fetch ``{title, body, labels}`` for an issue via ``gh``.

    Returns ``None`` on any fetch failure so callers can fail-open.
    """
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", str(number), "--json", "title,body,labels"],
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
    return {
        "title": data.get("title", "") or "",
        "body": data.get("body", "") or "",
        "labels": label_names,
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


def _format_remediation_comment(findings: list[IntakeFinding], replacement: str | None) -> str:
    lines = ["**TheForge intake remediation — proposed replacement**", ""]
    lines.append("Findings:")
    for f in findings:
        lines.append(f"- `{f.code}` ({f.location}): {f.problem}")
    if replacement:
        lines.append("")
        lines.append("Proposed replacement issue body:")
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


def _remediate_one(
    *,
    slug: str,
    issue_number: int,
    title: str,
    body: str,
    labels: list[str],
    grooming_enabled: bool,
    auto_fix_enabled: bool,
    auto_fix_mode: str,
    agent_caller: AgentRewriteCaller | None,
    post_comment: PostIssueComment,
    edit_body: EditIssueBody,
    project_root: Path | None,
) -> IntakeOutcome:
    """Run the full intake/auto-fix sequence for a single story."""
    findings = _gather_findings(title, body, labels, grooming_enabled=grooming_enabled)
    blocking = _blocking(findings)
    if not blocking:
        return IntakeOutcome(slug=slug, kind=IntakeOutcomeKind.PASSED, findings=tuple(findings))

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
        )

    # Even if mechanical patches resolved everything, the gate must rerun once
    # to verify. If no semantic findings remain, skip the agent call.
    proposed_body = patched_body
    if semantic_remaining:
        if agent_caller is None:
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_SHAPE,
                findings=tuple(blocking),
                detail="auto-fix enabled but no agent caller wired",
            )
        agent_output = agent_caller(patched_body, semantic_remaining)
        if not agent_output:
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
                findings=tuple(blocking),
                proposed_replacement=None,
                detail="agent remediation produced no output",
            )
        proposed_body = agent_output

    # Re-run shape + grooming once on the proposed body to verify resolution.
    rerun_findings = _gather_findings(
        title, proposed_body, patched_labels, grooming_enabled=grooming_enabled
    )
    rerun_blocking = _blocking(rerun_findings)

    if auto_fix_mode == "edit":
        if rerun_blocking:
            # Don't edit a body that still fails — surface for human triage.
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
                findings=tuple(rerun_blocking),
                proposed_replacement=proposed_body,
                detail="rerun gate still failing; did not edit issue",
            )
        if not edit_body(issue_number, proposed_body, project_root):
            return IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
                findings=tuple(blocking),
                proposed_replacement=proposed_body,
                detail="gh issue edit failed",
            )
        return IntakeOutcome(
            slug=slug,
            kind=IntakeOutcomeKind.REMEDIATED,
            findings=tuple(blocking),
            proposed_replacement=proposed_body,
            detail="edit mode: issue body updated, gate rerun passed",
        )

    # Default: comment mode. Post the proposed replacement and drop the story
    # from this sprint. The contract is unchanged on disk; the operator
    # decides whether to accept the rewrite.
    comment = _format_remediation_comment(blocking, proposed_body)
    post_comment(issue_number, comment, project_root)
    if rerun_blocking:
        return IntakeOutcome(
            slug=slug,
            kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
            findings=tuple(rerun_blocking),
            proposed_replacement=proposed_body,
            detail="comment mode: posted replacement; rerun gate still failing",
        )
    return IntakeOutcome(
        slug=slug,
        kind=IntakeOutcomeKind.DROPPED_SHAPE,
        findings=tuple(blocking),
        proposed_replacement=proposed_body,
        detail="comment mode: proposed replacement posted; story dropped",
    )


def run_intake_remediation(
    tasks: list[TaskStory],
    project_root: Path | None,
    *,
    grooming_enabled: bool = False,
    auto_fix_enabled: bool = False,
    auto_fix_mode: str = "comment",
    agent_caller: AgentRewriteCaller | None = None,
    fetch_detail: FetchIssueDetail = _default_fetch_detail,
    post_comment: PostIssueComment = _default_post_comment,
    edit_body: EditIssueBody = _default_edit_body,
) -> dict[str, IntakeOutcome]:
    """Run the intake remediation pass over the full normalized task list.

    Returns a slug → ``IntakeOutcome`` map covering every task. Tasks without
    a linked GitHub issue are returned as ``PASSED`` with empty findings —
    the grooming check is GH-only because it needs the live issue body.

    When both ``grooming_enabled`` and ``auto_fix_enabled`` are False, this is
    a near no-op: shape findings are still collected for parity but no
    remediation runs and no story is dropped here. (The shape gate already
    filtered upstream in ``cli/sprint.py``.)
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
            grooming_enabled=grooming_enabled,
            auto_fix_enabled=auto_fix_enabled,
            auto_fix_mode=auto_fix_mode,
            agent_caller=agent_caller,
            post_comment=post_comment,
            edit_body=edit_body,
            project_root=project_root,
        )

    return outcomes
