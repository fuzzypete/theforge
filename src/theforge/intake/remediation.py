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

import datetime
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


# Shape-gate skip reasons whose remediation is a body edit. The shape gate's
# own failure-message hint must literally describe a body change for inclusion
# here — declining a body-fixable reason would defeat the purpose of body-only
# intake automation. Keep this set in sync with new shape-gate skip codes.
BODY_FIXABLE_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "reopened_stale_contract",
    }
)


class ShapeGateSkipRemediationKind(str, Enum):
    """Per-issue outcome of remediating a shape-gate skip reason."""

    REMEDIATED = "remediated"  # body edited; gate rerun passes
    DROPPED_AFTER_FIX = "dropped_after_fix"  # body edit attempted; gate still fails
    DROPPED_NOT_COVERED = "dropped_not_covered"  # skip reason not body-fixable
    DROPPED_DISABLED = "dropped_disabled"  # auto_fix disabled or wrong mode
    DROPPED_FETCH = "dropped_fetch"  # could not refetch issue detail
    DROPPED_EDIT = "dropped_edit"  # gh edit call failed


@dataclass(frozen=True)
class ShapeGateSkipRemediation:
    """Result of attempting body-only remediation for a shape-gate skip."""

    issue_number: int
    reason_codes: tuple[str, ...]
    kind: ShapeGateSkipRemediationKind
    detail: str = ""
    new_body: str | None = None

    def as_dict(self) -> dict:
        return {
            "issue_number": self.issue_number,
            "reason_codes": list(self.reason_codes),
            "kind": self.kind.value,
            "detail": self.detail,
            "new_body": self.new_body,
        }


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
    candidate_artifact_path: str | None = None,
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
        "candidate_artifact_path": candidate_artifact_path,
    }


def _write_candidate_artifact(
    *,
    issue_number: int,
    candidate_body: str,
    findings: list[IntakeFinding],
    project_root: Path | None,
) -> str | None:
    """Persist the failed-rerun candidate to a durable artifact file.

    Used as a fallback when the issue-comment post fails. Returns the
    written path (as a string) on success, or ``None`` if the artifact
    could not be written. The artifact lives under
    ``.forge/intake/candidates/`` so the operator can discover it without
    grepping logs.
    """
    root = project_root if project_root is not None else Path.cwd()
    artifact_dir = root / ".forge" / "intake" / "candidates"
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_path = artifact_dir / f"issue-{issue_number}-{timestamp}.md"
    rendered = _format_remediation_comment(
        findings,
        candidate_body,
        header="**TheForge intake remediation — candidate failed rerun gate**",
        findings_label="Remaining blocking findings against the candidate body:",
        replacement_label=(
            "Candidate replacement body (rerun gate still failing — operator review required):"
        ),
    )
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "failed to write intake candidate artifact for issue #%s: %s",
            issue_number,
            exc,
        )
        return None
    return str(artifact_path)


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
            artifact_path: str | None = None
            if not posted:
                # Fallback persistence: comment post failed, so the operator
                # cannot find the candidate on the issue thread. Write a
                # durable artifact instead so proposed_replacement is never
                # silently lost.
                artifact_path = _write_candidate_artifact(
                    issue_number=issue_number,
                    candidate_body=proposed_body,
                    findings=rerun_blocking,
                    project_root=project_root,
                )
            if posted:
                detail = (
                    "rerun gate still failing; candidate posted as issue comment "
                    "for operator review"
                )
            elif artifact_path is not None:
                detail = (
                    "rerun gate still failing; comment post failed — candidate "
                    f"persisted to {artifact_path}"
                )
            else:
                detail = (
                    "rerun gate still failing; comment post failed and candidate "
                    "artifact write failed"
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
                    candidate_artifact_path=artifact_path,
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


def remediate_shape_gate_skip(
    *,
    issue_number: int,
    reason_codes: tuple[str, ...],
    project_root: Path | None,
    auto_fix_enabled: bool,
    auto_fix_mode: str,
    fetch_detail: Callable[[int, Path | None], dict | None] | None = None,
    edit_body: EditIssueBody = _default_edit_body,
) -> ShapeGateSkipRemediation:
    """Attempt body-only remediation for a shape-gate-skipped issue.

    Today this covers ``reopened_stale_contract``: the body-fixable repair
    is to fold the reopen-context comment into the issue body. The shape
    gate's clearance signal — body's ``lastEditedAt`` newer than the
    triggering reopen-comment timestamp — naturally clears once
    ``gh issue edit`` runs.

    Returns a :class:`ShapeGateSkipRemediation` describing the outcome. The
    caller is responsible for moving REMEDIATED issues back into the
    runnable set and surfacing the others in the sprint warning output.
    """
    # The shape gate emits one skip reason per issue today, but accept
    # multiple for forward-compat. Pick the first body-fixable code.
    body_fixable = [c for c in reason_codes if c in BODY_FIXABLE_SKIP_REASONS]
    if not body_fixable:
        return ShapeGateSkipRemediation(
            issue_number=issue_number,
            reason_codes=reason_codes,
            kind=ShapeGateSkipRemediationKind.DROPPED_NOT_COVERED,
            detail=(f"shape-gate skip reason {reason_codes!r} is not in the body-fixable set"),
        )
    if not auto_fix_enabled or auto_fix_mode != "edit":
        return ShapeGateSkipRemediation(
            issue_number=issue_number,
            reason_codes=reason_codes,
            kind=ShapeGateSkipRemediationKind.DROPPED_DISABLED,
            detail=(
                "intake auto_fix disabled or not in 'edit' mode "
                f"(enabled={auto_fix_enabled}, mode={auto_fix_mode!r})"
            ),
        )

    # Lazy import to keep this module importable from contexts that don't
    # need the shape-gate machinery.
    from ..sprint.reopen_context import analyze_reopen_contract, append_reopen_context
    from ..sprint.shape_gate import _fetch_issue_detail

    fetch = fetch_detail if fetch_detail is not None else _fetch_issue_detail
    detail = fetch(issue_number, project_root)
    if detail is None:
        return ShapeGateSkipRemediation(
            issue_number=issue_number,
            reason_codes=reason_codes,
            kind=ShapeGateSkipRemediationKind.DROPPED_FETCH,
            detail="could not fetch issue detail",
        )

    primary_code = body_fixable[0]
    if primary_code != "reopened_stale_contract":
        # Future body-fixable codes: dispatch by code here.
        return ShapeGateSkipRemediation(
            issue_number=issue_number,
            reason_codes=reason_codes,
            kind=ShapeGateSkipRemediationKind.DROPPED_NOT_COVERED,
            detail=f"body-fixable code {primary_code!r} has no remediator wired yet",
        )

    state = analyze_reopen_contract(detail, detail.get("timeline") or [])
    if not state.has_reopen_context:
        return ShapeGateSkipRemediation(
            issue_number=issue_number,
            reason_codes=reason_codes,
            kind=ShapeGateSkipRemediationKind.DROPPED_AFTER_FIX,
            detail="no reopen context found on refetch — race or stale skip record",
        )

    new_body = append_reopen_context(detail.get("body", "") or "", state)
    if new_body == (detail.get("body", "") or ""):
        return ShapeGateSkipRemediation(
            issue_number=issue_number,
            reason_codes=reason_codes,
            kind=ShapeGateSkipRemediationKind.DROPPED_AFTER_FIX,
            detail="reopen-context block already present; body edit would be a no-op",
            new_body=new_body,
        )

    if not edit_body(issue_number, new_body, project_root):
        return ShapeGateSkipRemediation(
            issue_number=issue_number,
            reason_codes=reason_codes,
            kind=ShapeGateSkipRemediationKind.DROPPED_EDIT,
            detail="gh issue edit failed",
            new_body=new_body,
        )

    return ShapeGateSkipRemediation(
        issue_number=issue_number,
        reason_codes=reason_codes,
        kind=ShapeGateSkipRemediationKind.REMEDIATED,
        detail="reopen-context comment folded into body; gate clearance signal updated",
        new_body=new_body,
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
    force: bool = False,
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

    if force:
        # Operator override: --force is a uniform, single-layer bypass of
        # intake gating. Skip every per-task remediation step (including the
        # GH detail fetch) so no LLM auto-fix budget is spent and the issue
        # progresses to preflight regardless of which gate rule would fire.
        for task in tasks:
            outcomes[task.slug] = IntakeOutcome(
                slug=task.slug,
                kind=IntakeOutcomeKind.PASSED,
                detail="intake remediation bypassed by --force",
                audit={"force_bypass": True},
            )
        return outcomes

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
