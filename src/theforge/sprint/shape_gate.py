"""Sprint-entry shape gate — label check + local shape_check re-run.

Filters issues before preflight spends money on them. Defense-in-depth behind
the #811 GitHub Action: catches stale ``needs-grooming`` labels, issues that
existed before the Action was deployed, and edits made while the Action was
offline.

Pure orchestration over ``shape_check`` and ``gh`` CLI. Fail-closed on
unreachable ``gh`` calls: an issue whose labels/body cannot be fetched is
left runnable (the pre-existing sources.py fetch will surface any real
error). We do not want the shape gate itself to be a new source of silent
drops.

The admissibility decision itself lives in ``theforge.admissibility`` so that
operator-facing surfaces which advertise sprint-eligible work (``forge status
--ready``) render the same verdict this gate enforces (#2027).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..admissibility import (
    NEEDS_GROOMING_LABEL,
    OPERATOR_ACTION_CODE,
    OPERATOR_ACTION_CONFLICT_LABELS,
    OPERATOR_ACTION_LABEL,
    OPERATOR_ACTION_LABEL_CONFLICT_CODE,
    OPERATOR_ACTION_MISSING_AC_CODE,
    advisory_reasons,
    classify_admissibility,
    has_acceptance_criteria,
    resolve_classifier,
)
from ..shape_check import ShapeVerdict
from .reopen_context import analyze_reopen_contract, format_reopen_stale_detail

_log = logging.getLogger(__name__)

VerdictEmitter = Callable[[dict], None]

REOPENED_STALE_CONTRACT_CODE = "reopened_stale_contract"

# Aliases for helpers that moved to ``admissibility``: the gate stays the
# import site callers already know.
_has_acceptance_criteria = has_acceptance_criteria
_resolve_classifier = resolve_classifier

__all__ = [
    "NEEDS_GROOMING_LABEL",
    "OPERATOR_ACTION_CODE",
    "OPERATOR_ACTION_CONFLICT_LABELS",
    "OPERATOR_ACTION_LABEL",
    "OPERATOR_ACTION_LABEL_CONFLICT_CODE",
    "OPERATOR_ACTION_MISSING_AC_CODE",
    "REOPENED_STALE_CONTRACT_CODE",
    "ShapeGateResult",
    "SkippedIssue",
    "VerdictEmitter",
    "apply_shape_gate",
    "format_advisory_warning",
    "format_operator_action_notice",
    "format_skipped_warning",
    "skipped_issue_state_fields",
]

# Bot comments from #806b embed machine-readable reason codes on a single
# line so local re-runs can match the Action's verdict exactly. Accept either
# a simple CSV form or a JSON array.
_BOT_REASONS_RE = re.compile(
    r"<!--\s*shape-check-reasons:\s*(?P<payload>.+?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SkippedIssue:
    """A sprint-entry rejection record exposed to audit/summary output."""

    issue_number: int
    reason_codes: tuple[str, ...]
    source: str  # "label" or "local_check"
    title: str = ""
    detail: str = ""
    verdict: str = ShapeVerdict.NEEDS_OPERATOR_ACTION.value
    verdict_description: str = ""

    def as_dict(self) -> dict:
        return {
            "issue_number": self.issue_number,
            "reason_codes": list(self.reason_codes),
            "source": self.source,
            "title": self.title,
            "detail": self.detail,
            "verdict": self.verdict,
            "verdict_description": self.verdict_description,
        }


@dataclass(frozen=True)
class ShapeGateResult:
    runnable: list[dict] = field(default_factory=list)
    skipped: list[SkippedIssue] = field(default_factory=list)
    advisories: list[SkippedIssue] = field(default_factory=list)
    # Issues correctly labeled ``operator-action``: deliberate non-dispatch.
    # Distinct from ``skipped`` (wrong shape) so operators can tell "system
    # refused to run my malformed issue" from "system identified my issue as
    # operator work". Issues with ``operator-action`` plus a label conflict
    # or no AC section land in ``skipped`` instead.
    operator_action: list[SkippedIssue] = field(default_factory=list)


def skipped_issue_state_fields(skipped: object) -> tuple[str, dict]:
    """Return ``(reason, detail)`` for registering a SkippedIssue in canonical state.

    Centralizes the verdict-preferred reason resolution and the per-story
    ``detail`` shape so every caller (state_writer.write_bootstrap_state,
    cli.sprint._emit_all_skipped_audit, sprint.runner skipped registration)
    surfaces the typed verdict identifier consistently. forge sprint-status
    reads the canonical ``reason`` for the DETAIL column, so divergence here
    silently regresses the AC that verdicts appear in operator surfaces.
    """
    sk_dict = skipped.as_dict() if hasattr(skipped, "as_dict") else dict(skipped)
    codes = sk_dict.get("reason_codes") or []
    verdict = sk_dict.get("verdict")
    verdict_desc = sk_dict.get("verdict_description") or ""
    if verdict:
        reason = verdict
    elif codes:
        reason = ", ".join(codes)
    else:
        reason = sk_dict.get("detail") or sk_dict.get("source") or "shape-gate"
    detail = {
        "shape_gate_source": sk_dict.get("source"),
        "shape_gate_codes": list(codes),
        "shape_verdict": verdict,
        "shape_verdict_description": verdict_desc,
        "final_outcome": "SKIPPED",
    }
    return reason, detail


def _fetch_issue_detail(number: int, project_root: Path | None) -> dict | None:
    """Fetch issue detail for a single issue via ``gh``.

    Returns ``None`` on any fetch failure so callers can decide whether to
    fail-open (leave the issue runnable) or fail-closed.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--json",
                "title,body,labels,state,closedAt,stateReason,updatedAt,lastEditedAt,comments",
            ],
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
        "state": data.get("state", "OPEN"),
        "closedAt": data.get("closedAt"),
        "stateReason": data.get("stateReason"),
        "updatedAt": data.get("updatedAt"),
        "lastEditedAt": data.get("lastEditedAt"),
        "comments": data.get("comments") or [],
        "timeline": _fetch_issue_timeline(number, project_root),
    }


_TIMELINE_PER_PAGE = 100
_TIMELINE_MAX_PAGES = 20  # safety cap: 2000 events, far beyond any real issue


def _fetch_issue_timeline(number: int, project_root: Path | None) -> list[dict]:
    """Return raw issue timeline events across all pages, or [] when unavailable.

    Reopen-contract analysis depends on the full timeline: an issue with more
    than one page of events could silently lose its reopen event to
    truncation, so this follows pagination until a short page (or the empty
    tail) signals the end rather than stopping at a single fixed page.
    """
    events: list[dict] = []
    for page in range(1, _TIMELINE_MAX_PAGES + 1):
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"repos/{{owner}}/{{repo}}/issues/{number}/timeline"
                    f"?per_page={_TIMELINE_PER_PAGE}&page={page}",
                ],
                capture_output=True,
                text=True,
                cwd=str(project_root) if project_root else None,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            return events if page > 1 else []
        if proc.returncode != 0:
            return events if page > 1 else []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return events if page > 1 else []
        if not isinstance(data, list):
            return events if page > 1 else []
        events.extend(item for item in data if isinstance(item, dict))
        if len(data) < _TIMELINE_PER_PAGE:
            break
    else:
        _log.warning(
            "issue #%s timeline exceeded %s pages (%s events); "
            "reopen analysis may be missing later events",
            number,
            _TIMELINE_MAX_PAGES,
            len(events),
        )
    return events


def _fetch_bot_reason_codes(number: int, project_root: Path | None) -> list[str]:
    """Return reason codes embedded in the #806b shape-check bot comment.

    Looks for a machine-readable marker of the form
    ``<!-- shape-check-reasons: code1,code2 -->`` or a JSON array. Returns an
    empty list when no bot comment is found or it cannot be parsed — callers
    should re-derive locally in that case.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--comments",
                "--json",
                "comments",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root) if project_root else None,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    for comment in reversed(data.get("comments") or []):
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        match = _BOT_REASONS_RE.search(body)
        if match is None:
            continue
        payload = match.group("payload").strip()
        # JSON array form
        if payload.startswith("["):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(c).strip() for c in parsed if str(c).strip()]
        # CSV form
        return [part.strip() for part in payload.split(",") if part.strip()]
    return []


def _noop_emit_verdict(_event: dict) -> None:
    return None


def _local_advisory_entry(
    issue_number: int,
    title: str,
    reason_pairs: list[tuple[str, str]],
) -> SkippedIssue | None:
    if not reason_pairs:
        return None
    codes = tuple(code for code, _detail in reason_pairs)
    detail = "; ".join(detail for _code, detail in reason_pairs if detail)
    return SkippedIssue(
        issue_number=issue_number,
        reason_codes=codes,
        source="local_check",
        title=title,
        detail=detail,
        verdict="",
        verdict_description="",
    )


def apply_shape_gate(
    issues: list[dict],
    project_root: Path | None,
    *,
    classifier_mode: str = "heuristic",
    force: bool = False,
    fetch_detail=_fetch_issue_detail,
    llm_caller=None,
    emit_verdict: VerdictEmitter = _noop_emit_verdict,
    intake_remediated_numbers: "set[int] | frozenset[int] | None" = None,
) -> ShapeGateResult:
    """Partition issues into runnable vs skipped before preflight runs.

    Algorithm:

    1. For each ``{number, title}`` in *issues*, fetch labels + body.
    2. Re-run the local shape check against the current body and labels for
       every issue (the defense-in-depth step that closes the stale-comment
       loophole).
    3. If ``needs-grooming`` is present, skip with ``source='label'`` while
       surfacing the live blocking findings that explain the current state.
    4. Otherwise, if the check returns a non-``RUNNABLE`` shape, skip with
       ``source='local_check'``.

    ``force=True`` returns every input issue as runnable but still populates
    ``skipped`` so the CLI can surface a prominent warning listing reasons.

    ``intake_remediated_numbers`` lists issues whose bodies this run just
    authoritatively edited via intake remediation. The async ``needs-grooming``
    relabeler workflow may not have caught up by re-exec time; for these
    issues the label is treated as async-stale and the live local check is
    trusted instead. Without this suppression, a sprint that successfully
    remediates an issue and then re-execs (source-pull mid-run) drops the
    just-remediated issue on the post-re-exec gate pass.
    """
    remediated = frozenset(intake_remediated_numbers or ())
    runnable: list[dict] = []
    skipped: list[SkippedIssue] = []
    advisories: list[SkippedIssue] = []
    operator_action: list[SkippedIssue] = []
    # Tracks every issue number that carried the operator-action label,
    # regardless of how it was classified (clean operator-action, label
    # conflict, or missing-AC). --force must exclude all of them — the
    # label is the operator's deliberate signal that no dev cycle should
    # run for this issue, not a guard --force can override.
    operator_action_label_numbers: set[int] = set()

    def _safe_emit(payload: dict) -> None:
        # Substrate emission is observability, not gating: a write failure
        # must not block the sprint. Log at WARNING so operators notice but
        # the gate continues to partition issues exactly as before.
        try:
            emit_verdict(payload)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "shape-gate verdict substrate write failed for issue=%s verdict=%s: %s",
                payload.get("issue_id"),
                payload.get("verdict"),
                exc,
            )

    for issue in issues:
        number = int(issue["number"])
        title_short = issue.get("title", "")
        detail = fetch_detail(number, project_root)
        if detail is None:
            # Fail-open: let the downstream source.fetch surface the real error.
            runnable.append(issue)
            continue

        labels = detail["labels"]
        title = detail["title"] or title_short
        body = detail["body"]

        # The admissibility decision (operator-action short-circuit, the live
        # shape_check run, the needs-grooming label, and the verdict/shape
        # branches) is shared with the ready-queue surface so both agree on
        # what may enter a sprint.
        adm = classify_admissibility(
            title,
            body,
            labels,
            classifier_mode=classifier_mode,
            llm_caller=llm_caller,
            trust_local_over_grooming_label=number in remediated,
        )

        # operator-action: refused ahead of the regular shape check. An
        # operator-action issue is not malformed; it is correctly identified as
        # work no dev agent can perform. Running the standard shape check on it
        # would flag it for missing_type (since operator-action is mutually
        # exclusive with bug/enhancement/epic/task), conflating two distinct
        # operator signals. Refuse here with a label-source skip and stop.
        if adm.operator_action_label:
            operator_action_label_numbers.add(number)
            entry = SkippedIssue(
                issue_number=number,
                reason_codes=adm.reason_codes,
                source=adm.source,
                title=title_short,
                detail=adm.detail,
            )
            if adm.deliberate_non_dispatch:
                operator_action.append(entry)
            else:
                skipped.append(entry)
            continue

        reopen_state = analyze_reopen_contract(detail, detail.get("timeline") or [])
        local_advisory = (
            _local_advisory_entry(number, title_short, advisory_reasons(adm.result))
            if adm.result is not None
            else None
        )

        if not adm.admissible and adm.source == "label":
            skipped.append(
                SkippedIssue(
                    issue_number=number,
                    reason_codes=adm.reason_codes,
                    source="label",
                    title=title_short,
                    detail=adm.detail,
                    verdict=adm.verdict,
                    verdict_description=adm.verdict_description,
                )
            )
            _safe_emit(
                {
                    "issue_id": str(number),
                    "verdict": adm.verdict,
                    "source": "label",
                    "reason_codes": list(adm.reason_codes),
                }
            )
            continue

        if reopen_state.has_stale_body:
            # Advisory only: the underlying check verifies timeline-event
            # ordering, not body content, so blocking sprint entry on it
            # would demand ceremonial work the gate cannot validate. The
            # reopen comment itself flows to dev/review via
            # ``append_reopen_context`` in ``sources.py`` regardless.
            # Non-blocking, so no typed verdict is claimed here and no
            # verdict event is emitted; the issue continues through the
            # checks below and receives its verdict (and substrate emission)
            # from whichever branch ultimately classifies it.
            advisories.append(
                SkippedIssue(
                    issue_number=number,
                    reason_codes=(REOPENED_STALE_CONTRACT_CODE,),
                    source="local_check",
                    title=title_short,
                    detail=format_reopen_stale_detail(reopen_state),
                    verdict="",
                    verdict_description="",
                )
            )

        # Refusals from the live check: a non-RUNNABLE shape, or the
        # diagnosis_cause_unknown verdict, which is admissible as a *shape*
        # (Shape.RUNNABLE) but not implementation-runnable per ADR-0001. The
        # verdict is the routing signal; map_shape's binary view is not.
        if not adm.admissible:
            skipped.append(
                SkippedIssue(
                    issue_number=number,
                    reason_codes=adm.reason_codes,
                    source="local_check",
                    title=title_short,
                    detail=adm.detail,
                    verdict=adm.verdict,
                    verdict_description=adm.verdict_description,
                )
            )
            _safe_emit(
                {
                    "issue_id": str(number),
                    "verdict": adm.verdict,
                    "source": "local_check",
                    "reason_codes": list(adm.reason_codes),
                }
            )
            continue

        # Runnable: attach the verdict to the dict so downstream audit/summary
        # can render it (instrumentation per CONVENTIONS rule 6).
        issue_with_verdict = dict(issue)
        issue_with_verdict["shape_verdict"] = ShapeVerdict.RUNNABLE.value
        if local_advisory is not None:
            advisories.append(local_advisory)
            issue_with_verdict["shape_advisories"] = list(local_advisory.reason_codes)
        runnable.append(issue_with_verdict)
        _safe_emit(
            {
                "issue_id": str(number),
                "verdict": ShapeVerdict.RUNNABLE.value,
                "source": "local_check",
                "reason_codes": [],
            }
        )

    if force:
        # Escape hatch: caller wants to run every input issue. Skip list is
        # preserved so CLI can render a prominent warning. operator-action is
        # NOT bypassed by --force regardless of how the label combined with
        # other shape signals: clean operator-action, operator-action with a
        # type-label conflict, and operator-action without an AC section all
        # remain refused. The label is a deliberate operator signal, not a
        # guard --force can override. CLI still renders the
        # operator_action banner and the skipped warning.
        force_runnable = [
            issue for issue in issues if int(issue["number"]) not in operator_action_label_numbers
        ]
        return ShapeGateResult(
            runnable=force_runnable,
            skipped=skipped,
            advisories=advisories,
            operator_action=operator_action,
        )

    return ShapeGateResult(
        runnable=runnable,
        skipped=skipped,
        advisories=advisories,
        operator_action=operator_action,
    )


def format_skipped_warning(skipped: list[SkippedIssue]) -> str:
    """Render a human-readable warning for the skipped issue list."""
    if not skipped:
        return ""
    lines = [f"[forge] {len(skipped)} issue(s) flagged by shape gate:"]
    for entry in skipped:
        codes = ", ".join(entry.reason_codes) or "<no codes>"
        title = f" — {entry.title}" if entry.title else ""
        detail = f" [{entry.detail}]" if entry.detail else ""
        verdict = f" verdict={entry.verdict}" if entry.verdict else ""
        lines.append(
            f"  - #{entry.issue_number} ({entry.source}):{verdict} {codes}{title}{detail}"
        )
    return "\n".join(lines)


def format_operator_action_notice(operator_action: list[SkippedIssue]) -> str:
    """Render the deliberate-non-dispatch banner for operator-action issues.

    Phrased as "deliberately non-dispatched" to make clear this is not a
    failure mode but the system correctly identifying operator deliverables.
    """
    if not operator_action:
        return ""
    lines = [
        f"[forge] {len(operator_action)} issue(s) deliberately non-dispatched (operator-action):"
    ]
    for entry in operator_action:
        title = f" — {entry.title}" if entry.title else ""
        lines.append(f"  - #{entry.issue_number} (label): {OPERATOR_ACTION_LABEL}{title}")
    return "\n".join(lines)


def format_advisory_warning(advisories: list[SkippedIssue]) -> str:
    """Render a human-readable advisory note for non-blocking findings."""
    if not advisories:
        return ""
    lines = [f"[forge] {len(advisories)} issue(s) carry shape-gate advisories (not blocking):"]
    for entry in advisories:
        codes = ", ".join(entry.reason_codes) or "<no codes>"
        title = f" — {entry.title}" if entry.title else ""
        detail = f" [{entry.detail}]" if entry.detail else ""
        lines.append(f"  - #{entry.issue_number} ({entry.source}): {codes}{title}{detail}")
    return "\n".join(lines)
