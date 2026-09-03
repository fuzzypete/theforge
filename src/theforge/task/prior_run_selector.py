"""Deterministic selection of prior-run summaries for context assembly.

This is the consumer half of Layer 3 in ``docs/plans/knowledge-capture.md``.
``theforge.knowledge_index`` materializes the deterministic view over persisted
summaries — including the admissibility verdict — and this module decides which
of those entries a *later* run is allowed to see.

Two rules shape everything here:

1. **Relevance is scored from deterministic fields only.** File paths, domains,
   story shape, indexed pattern tags, and ``generated_at`` recency all come from
   the coordinator, not from the model that wrote the summary. LLM-authored
   prose (``what_changed``, ``what_was_learned``) is *rendered* for an agent to
   read only after eligibility has already been decided, so no sentence a model
   wrote can move a summary into a prompt it would not otherwise reach.
2. **Absence of a verdict is a verdict.** ``interpret_persisted_verdict`` fails
   closed, so an entry written before #1866, or one whose verdict block is
   malformed, is excluded as ``inadmissible(no_verdict)`` rather than admitted
   on the benefit of the doubt.

Every failure mode — missing index, unparseable YAML, wrong schema version,
unreadable summary artifact — degrades to "no candidates", never to an
exception. A run with nothing to learn from must proceed normally.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from theforge.knowledge_admissibility import (
    RANK_REDUCED,
    STATUS_ADMISSIBLE,
    STATUS_ADMISSIBLE_WITH_REDUCED_RANK,
    KnowledgeSummaryVerdict,
    interpret_persisted_verdict,
)
from theforge.knowledge_index import (
    KNOWLEDGE_INDEX_PATH,
    KNOWLEDGE_INDEX_SCHEMA_VERSION,
    rebuild_knowledge_index,
)

#: ``ContextItem.kind`` for an injected prior-run summary. Budget accounting and
#: the audit manifest both branch on this value.
PRIOR_RUN_KIND = "prior_run_summary"

#: ADR-0002 clause 5: summary prose may advise the planning, development, and
#: review agents. Preflight may receive only audit-derived signal renderings,
#: never summary prose, because its output (sufficiency, complexity, likely
#: files, refusal) drives coordinator control flow.
ELIGIBLE_PHASES = frozenset({"plan", "dev", "review"})
SIGNAL_ONLY_PHASES = frozenset({"preflight"})
SUPPORTED_PHASES = ELIGIBLE_PHASES | SIGNAL_ONLY_PHASES

#: Most prior summaries a single assembly will ever offer, before budget.
_MAX_CANDIDATES = 3

#: Recency is a tie-breaker, not a relevance signal in its own right.
_RECENCY_WINDOW = 3

_SCORE_FILE_OVERLAP = 10
_SCORE_DIR_OVERLAP = 4
_SCORE_DOMAIN_MATCH = 6
_SCORE_WORK_TYPE_MATCH = 3
_SCORE_STORY_MATCH = 2
_SCORE_PATTERN_MATCH = 2
_SCORE_RECENT = 2
_PENALTY_REDUCED_RANK = 5

_MAX_REASON_DETAILS = 3
_MAX_RENDERED_CLAIMS = 5
_MAX_RENDERED_PATTERNS = 5
_MAX_FIELD_CHARS = 400
_RENDERED_SIZE_UNIT = "tokens"
_RENDERED_SIZE_METHOD = "word_punctuation_estimate_v1"
_RENDERED_SIZE_KIND = "rendered_prompt_contribution"

# Tokens that overlap between any two stories in this repository and therefore
# carry no relevance signal. Kept deliberately small: this is noise reduction,
# not a stopword corpus.
_NOISE_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "to",
        "for",
        "of",
        "in",
        "on",
        "is",
        "it",
        "add",
        "fix",
        "run",
        "test",
        "tests",
        "py",
        "src",
        "with",
        "that",
        "this",
        "not",
        "be",
        "by",
        "as",
        "or",
    }
)


def claim_reference(run_id: str, claim: str) -> str:
    """A stable, content-addressed handle for one rendered claim.

    Derived from the source run plus the claim text so the same claim rendered
    into two phases of the same run resolves to the same reference, and a
    reference stays resolvable after the fact without re-reading the summary.

    It lives beside the renderer rather than beside the manifest because the
    reference is now *displayed* to the agent (#2866): the string an agent can
    cite and the string the exposure record stores have to be produced by the
    same function, or a receipt could name a claim the record cannot find.
    """
    digest = hashlib.sha256(claim.strip().encode("utf-8")).hexdigest()[:12]
    return f"{run_id}:{digest}"


INDEX_STATE_READY = "ready"
INDEX_STATE_MISSING = "missing"
INDEX_STATE_UNREADABLE = "unreadable"
INDEX_STATE_STALE_SCHEMA = "stale_schema"


@dataclass(frozen=True)
class RenderedSummarySize:
    """The measured prompt footprint of one rendered prior-run summary."""

    value: int | None
    unit: str = _RENDERED_SIZE_UNIT
    method: str = _RENDERED_SIZE_METHOD
    kind: str = _RENDERED_SIZE_KIND
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "value": self.value,
            "unit": self.unit,
            "method": self.method,
            "kind": self.kind,
        }
        if self.unavailable_reason:
            payload["unavailable_reason"] = self.unavailable_reason
        return payload


@dataclass(frozen=True)
class PriorRunCandidate:
    """One prior summary that is eligible to be offered to an agent.

    ``claims`` is the *exact* claim list that survived rendering — same helpers,
    same ``_MAX_RENDERED_CLAIMS`` truncation, produced in the same pass that
    built ``content``. It is carried structurally rather than re-derived because
    a recorded claim has to be a claim the agent actually saw; re-deriving it
    downstream would let the record drift from the prompt (#2684).
    """

    run_id: str
    summary_path: str
    score: int
    reason: str
    verdict: KnowledgeSummaryVerdict
    content: str
    phase: str
    rendering_mode: str
    rendered_size: RenderedSummarySize
    claims: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return f"knowledge:{self.run_id}"


@dataclass(frozen=True)
class PriorRunExclusion:
    """One prior summary that existed but will not be offered, and why."""

    run_id: str
    reason: str
    verdict: KnowledgeSummaryVerdict | None = None
    admissibility_excluded: bool = False


@dataclass(frozen=True)
class PriorRunSelection:
    """The full deterministic verdict of one selection pass."""

    candidates: tuple[PriorRunCandidate, ...] = ()
    excluded: tuple[PriorRunExclusion, ...] = ()
    entry_count: int = 0
    index_state: str = INDEX_STATE_READY
    phase_eligible: bool = True
    phase: str = ""
    rendering_mode: str = ""


def select_prior_runs(
    project_root: Path,
    *,
    phase: str,
    story_text: str,
    file_list: list[str] | None = None,
    limit: int = _MAX_CANDIDATES,
) -> PriorRunSelection:
    """Choose the prior-run summaries a phase may be advised by.

    Returns an empty selection — never raises — when the phase is ineligible,
    the index is absent/malformed, or nothing scores above zero.
    """
    normalized_phase = (phase or "").lower()
    rendering_mode = _rendering_mode_for_phase(normalized_phase)
    if not rendering_mode:
        return PriorRunSelection(phase_eligible=False, phase=normalized_phase)

    entries, index_state = _load_index_entries(Path(project_root))
    if not entries:
        return PriorRunSelection(
            entry_count=0,
            index_state=index_state,
            phase_eligible=True,
            phase=normalized_phase,
            rendering_mode=rendering_mode,
        )

    recent_run_ids = _recent_run_ids(entries)
    story_terms = _tokens(story_text)
    touched_files, touched_dirs = _touched_paths(file_list)

    scored: list[PriorRunCandidate] = []
    excluded: list[PriorRunExclusion] = []

    for entry in entries:
        run_id = _text(entry.get("run_id"))
        if not run_id:
            continue

        verdict = interpret_persisted_verdict(entry.get("admissibility_verdict"))

        # Relevance is judged *before* admissibility, and the order is load-bearing
        # for the manifest rather than for safety. "2 summaries matched but were
        # excluded on admissibility" is a claim about knowledge this story could
        # have used; an inadmissible summary about an unrelated part of the
        # codebase was never that, and reporting it as withheld knowledge would
        # tell an operator something false. Either way the entry is excluded —
        # admissibility below is still an absolute bar on inclusion.
        primary_score, boost_score, reasons = _score_entry(
            entry,
            story_terms=story_terms,
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )
        if primary_score <= 0:
            excluded.append(
                PriorRunExclusion(run_id=run_id, reason="not_relevant", verdict=verdict)
            )
            continue

        if verdict.status not in (STATUS_ADMISSIBLE, STATUS_ADMISSIBLE_WITH_REDUCED_RANK):
            excluded.append(
                PriorRunExclusion(
                    run_id=run_id,
                    reason=_inadmissible_reason(verdict),
                    verdict=verdict,
                    admissibility_excluded=True,
                )
            )
            continue

        score = primary_score + boost_score
        if run_id in recent_run_ids:
            score += _SCORE_RECENT
            reasons.append("recent")

        if verdict.rank == RANK_REDUCED:
            # A down-ranked verdict says part of the summary's subject-matter
            # evidence no longer holds, so the penalty is charged against that
            # evidence alone — a boost as generic as "it is recent" must not
            # rescue a summary whose only real link to this story is weak.
            if primary_score <= _PENALTY_REDUCED_RANK:
                excluded.append(
                    PriorRunExclusion(
                        run_id=run_id,
                        reason=_stale_reason(verdict),
                        verdict=verdict,
                    )
                )
                continue
            score -= _PENALTY_REDUCED_RANK
            reasons.append("reduced_rank")

        content, rendered_claims = _render_summary(
            Path(project_root),
            entry,
            run_id=run_id,
            verdict=verdict,
            phase=normalized_phase,
            rendering_mode=rendering_mode,
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )
        if not content:
            excluded.append(
                PriorRunExclusion(run_id=run_id, reason="summary_unreadable", verdict=verdict)
            )
            continue

        scored.append(
            PriorRunCandidate(
                run_id=run_id,
                summary_path=_text(entry.get("summary_path")),
                score=score,
                reason=", ".join(reasons),
                verdict=verdict,
                content=content,
                phase=normalized_phase,
                rendering_mode=rendering_mode,
                rendered_size=_measure_rendered_summary(content),
                claims=tuple(rendered_claims),
            )
        )

    scored.sort(key=lambda candidate: (-candidate.score, candidate.run_id))
    kept = scored[: max(0, limit)]
    for overflow in scored[max(0, limit) :]:
        # These *did* match — they lost to better-scoring matches, which is a
        # different fact from "not relevant" and must read as one in the manifest.
        excluded.append(
            PriorRunExclusion(
                run_id=overflow.run_id,
                reason=f"below_selection_cap({limit})",
                verdict=overflow.verdict,
            )
        )

    excluded.sort(key=lambda item: item.run_id)
    return PriorRunSelection(
        candidates=tuple(kept),
        excluded=tuple(excluded),
        entry_count=len(entries),
        index_state=index_state,
        phase_eligible=True,
        phase=normalized_phase,
        rendering_mode=rendering_mode,
    )


# ── Index loading ─────────────────────────────────────────────────────────────


def _load_index_entries(project_root: Path) -> tuple[list[Mapping[str, Any]], str]:
    """Read the deterministic index, failing closed while reporting index health."""
    entries, index_state = _read_index_entries(project_root)
    if index_state == INDEX_STATE_READY:
        return entries, index_state

    try:
        rebuild_knowledge_index(project_root)
    except Exception:  # noqa: BLE001 - selector repair must degrade to the prior failure state
        return [], index_state

    repaired_entries, repaired_state = _read_index_entries(project_root)
    if repaired_state == INDEX_STATE_READY:
        return repaired_entries, repaired_state
    return [], index_state


def _read_index_entries(project_root: Path) -> tuple[list[Mapping[str, Any]], str]:
    """Read the deterministic index once, without attempting repair."""
    path = project_root / KNOWLEDGE_INDEX_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], INDEX_STATE_MISSING
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return [], INDEX_STATE_UNREADABLE
    if not isinstance(raw, Mapping):
        return [], INDEX_STATE_UNREADABLE
    if raw.get("schema_version") != KNOWLEDGE_INDEX_SCHEMA_VERSION:
        return [], INDEX_STATE_STALE_SCHEMA
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return [], INDEX_STATE_UNREADABLE
    if any(not isinstance(entry, Mapping) for entry in entries):
        return [], INDEX_STATE_UNREADABLE
    return entries, INDEX_STATE_READY


def _recent_run_ids(entries: list[Mapping[str, Any]]) -> frozenset[str]:
    """Return the run ids of the most recently generated indexed summaries."""
    ordered = sorted(
        entries,
        key=lambda entry: (_text(entry.get("generated_at")), _text(entry.get("run_id"))),
        reverse=True,
    )
    return frozenset(
        _text(entry.get("run_id")) for entry in ordered[:_RECENCY_WINDOW] if entry.get("run_id")
    )


# ── Scoring (deterministic fields only) ───────────────────────────────────────


def _score_entry(
    entry: Mapping[str, Any],
    *,
    story_terms: set[str],
    touched_files: set[str],
    touched_dirs: set[str],
) -> tuple[int, int, list[str]]:
    """Score one index entry as ``(primary, boost, reasons)``.

    *Primary* signals are subject-matter links to this story: overlapping files
    or directories, a matching domain, a matching story name, a matching pattern
    tag. *Boosts* — work type, recency — describe every entry in a repository
    equally well (half a backlog is ``work_type: feature``), so they only sharpen
    an ordering a primary signal already established. Without that split, a word
    as ordinary as "refactor" in the story text would qualify every refactor the
    project ever ran.
    """
    primary = 0
    boost = 0
    reasons: list[str] = []

    overlapping_files: list[str] = []
    overlapping_dirs: list[str] = []
    for changed in _string_list(entry.get("changed_files")):
        if changed in touched_files:
            overlapping_files.append(changed)
        elif str(Path(changed).parent) in touched_dirs:
            overlapping_dirs.append(str(Path(changed).parent))

    for path in _capped(sorted(set(overlapping_files))):
        primary += _SCORE_FILE_OVERLAP
        reasons.append(f"file_overlap({path})")
    for directory in _capped(sorted(set(overlapping_dirs))):
        primary += _SCORE_DIR_OVERLAP
        reasons.append(f"dir_overlap({directory})")

    for domain in _capped(sorted({d for d in _string_list(entry.get("domains")) if d})):
        if _tokens(domain) & story_terms:
            primary += _SCORE_DOMAIN_MATCH
            reasons.append(f"domain_match({domain})")

    story = entry.get("story")
    if isinstance(story, Mapping):
        story_tokens = _tokens(f"{_text(story.get('slug'))} {_text(story.get('name'))}")
        shared = story_tokens & story_terms
        if shared:
            primary += min(len(shared), _MAX_REASON_DETAILS) * _SCORE_STORY_MATCH
            reasons.append("story_match")

    for pattern in _capped(sorted({p for p in _string_list(entry.get("learned_patterns")) if p})):
        if _tokens(pattern) & story_terms:
            primary += _SCORE_PATTERN_MATCH
            reasons.append(f"pattern_match({pattern})")

    story_shape = entry.get("story_shape")
    if isinstance(story_shape, Mapping):
        work_type = _text(story_shape.get("work_type"))
        if work_type and _tokens(work_type) & story_terms:
            boost += _SCORE_WORK_TYPE_MATCH
            reasons.append(f"story_shape_match({work_type})")

    return primary, boost, reasons


def _touched_paths(file_list: list[str] | None) -> tuple[set[str], set[str]]:
    files = {str(Path(path)) for path in (file_list or []) if str(path).strip()}
    dirs = {str(Path(path).parent) for path in files}
    dirs.discard(".")
    return files, dirs


# ── Rendering (prose, only after eligibility) ─────────────────────────────────


def _render_summary(
    project_root: Path,
    entry: Mapping[str, Any],
    *,
    run_id: str,
    verdict: KnowledgeSummaryVerdict,
    phase: str,
    rendering_mode: str,
    touched_files: set[str],
    touched_dirs: set[str],
) -> tuple[str, list[str]]:
    """Render bounded advisory prose, and the claim list that prose contains.

    Returns ``(content, claims)``. ``claims`` is what an agent reading
    ``content`` was actually shown, so the two can never disagree about what
    was injected.
    """
    rel_path = _text(entry.get("summary_path"))
    if not rel_path:
        return ("", [])
    try:
        summary = yaml.safe_load((project_root / rel_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return ("", [])
    if not isinstance(summary, Mapping):
        return ("", [])

    lines = [
        f"## Prior run {run_id} (advisory — {verdict.status})",
        "",
        (
            "A previous run on related code produced the notes below. They are "
            "advisory context, not requirements, and may be out of date."
        ),
        "",
    ]
    phase_lines, claims = _phase_lines(
        phase,
        summary=summary,
        entry=entry,
        run_id=run_id,
        rendering_mode=rendering_mode,
        touched_files=touched_files,
        touched_dirs=touched_dirs,
    )
    lines.extend(phase_lines)

    if verdict.reasons:
        lines.append(f"- Verdict caveats: {', '.join(verdict.reasons)}")

    return ("\n".join(lines).strip(), claims)


def _phase_lines(
    phase: str,
    *,
    summary: Mapping[str, Any],
    entry: Mapping[str, Any],
    run_id: str,
    rendering_mode: str,
    touched_files: set[str],
    touched_dirs: set[str],
) -> tuple[list[str], list[str]]:
    """Return ``(prose lines, rendered claims)`` for one summary in one phase.

    Signal-only (preflight) rendering carries **no** claims by construction:
    ADR-0002 clause 5 keeps summary prose out of preflight entirely, so there
    is nothing an agent could have been told there.
    """
    if rendering_mode == "signal_only":
        return (_render_preflight_signals(summary), [])
    if phase == "plan":
        return _render_plan_summary(summary, run_id=run_id)
    if phase == "dev":
        return _render_dev_summary(
            summary,
            entry=entry,
            run_id=run_id,
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        )
    if phase == "review":
        return _render_review_summary(summary, run_id=run_id)
    return ([], [])


def _claim_line(run_id: str, claim: str) -> str:
    """Render one claim with the reference the exposure record already stores.

    The reference is displayed because a receipt has to cite an exposed claim
    without restating its prose (#2866); an agent that can only quote the text
    back cannot be matched against the record deterministically.
    """
    return f"  - [{claim_reference(run_id, claim)}] {claim}"


def _render_preflight_signals(summary: Mapping[str, Any]) -> list[str]:
    lines = [
        (
            "- Preflight note: Advisory prior-run signals only. "
            "Use these as risk context, not as control-flow input."
        ),
    ]
    story_shape = summary.get("story_shape")
    if isinstance(story_shape, Mapping):
        shape_parts = [
            f"{key}={value}"
            for key in ("work_type", "complexity", "complexity_score", "contract_change")
            if (value := story_shape.get(key)) is not None and _text(value)
        ]
        if shape_parts:
            lines.append(f"- Story shape: {', '.join(shape_parts)}")

    complexity = summary.get("complexity_signal")
    if isinstance(complexity, Mapping):
        metrics = []
        for key in ("actual_iterations", "review_cycles", "plan_regenerations", "cost_usd"):
            value = complexity.get(key)
            if value is None:
                continue
            metrics.append(f"{key}={value}")
        if metrics:
            lines.append(f"- Run signals: {', '.join(metrics)}")

    insights = summary.get("review_insights")
    if isinstance(insights, Mapping):
        recurring_count, recurring = _finding_metadata(insights.get("recurring_findings"))
        resolved_count, resolved = _finding_metadata(insights.get("resolved_findings"))
        if recurring_count:
            detail = f"; {', '.join(recurring)}" if recurring else ""
            lines.append(f"- Recurring findings: count={recurring_count}{detail}")
        if resolved_count:
            detail = f"; {', '.join(resolved)}" if resolved else ""
            lines.append(f"- Resolved findings: count={resolved_count}{detail}")
    return lines


def _render_plan_summary(
    summary: Mapping[str, Any], *, run_id: str
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    what_changed = summary.get("what_changed")
    if isinstance(what_changed, Mapping):
        approach = _text(what_changed.get("approach"), limit=_MAX_FIELD_CHARS)
        if approach:
            lines.append(f"- Prior approach: {approach}")
    rendered = _evidenced_claims(summary)[:_MAX_RENDERED_CLAIMS]
    if rendered:
        lines.append("- Lessons with resolved evidence:")
        lines.extend(_claim_line(run_id, claim) for claim in rendered)
    return (lines, rendered)


def _render_dev_summary(
    summary: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
    run_id: str,
    touched_files: set[str],
    touched_dirs: set[str],
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    changed_files = _string_list(summary.get("changed_files")) or _string_list(
        entry.get("changed_files")
    )
    if changed_files:
        lines.append(
            f"- Related changed files: {', '.join(changed_files[:_MAX_RENDERED_PATTERNS])}"
        )

    rendered = _dev_grounded_claims(
        summary, touched_files=touched_files, touched_dirs=touched_dirs
    )[:_MAX_RENDERED_CLAIMS]
    if rendered:
        lines.append("- Evidence-backed implementation patterns:")
        lines.extend(_claim_line(run_id, claim) for claim in rendered)
    return (lines, rendered)


def _render_review_summary(
    summary: Mapping[str, Any], *, run_id: str
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    rendered: list[str] = []
    insights = summary.get("review_insights")
    if isinstance(insights, Mapping):
        recurring = _finding_descriptions(insights.get("recurring_findings"))[
            :_MAX_RENDERED_CLAIMS
        ]
        resolved = _finding_descriptions(insights.get("resolved_findings"))[:_MAX_RENDERED_CLAIMS]
        observations = [
            _text(item, limit=_MAX_FIELD_CHARS)
            for item in _string_list(insights.get("observations"))
        ][:_MAX_RENDERED_CLAIMS]
        if recurring:
            lines.append("- Recurring findings to re-check:")
            lines.extend(_claim_line(run_id, item) for item in recurring)
        if resolved:
            lines.append("- Resolved findings worth verifying stayed fixed:")
            lines.extend(_claim_line(run_id, item) for item in resolved)
        if observations:
            lines.append("- Verification concerns:")
            lines.extend(_claim_line(run_id, item) for item in observations)
        rendered = [*recurring, *resolved, *observations]

    complexity = summary.get("complexity_signal")
    if isinstance(complexity, Mapping):
        metrics = []
        for key in ("review_cycles", "actual_iterations", "plan_regenerations"):
            value = complexity.get(key)
            if value is not None:
                metrics.append(f"{key}={value}")
        if metrics:
            lines.append(f"- Review-cycle signals: {', '.join(metrics)}")
    return (lines, rendered)


def _evidenced_claims(summary: Mapping[str, Any]) -> list[str]:
    learned = summary.get("what_was_learned")
    if not isinstance(learned, list):
        return []
    claims: list[str] = []
    for item in learned:
        if not isinstance(item, Mapping):
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            continue
        claim = _text(item.get("claim"), limit=_MAX_FIELD_CHARS)
        if claim:
            claims.append(claim)
    return claims


def _dev_grounded_claims(
    summary: Mapping[str, Any],
    *,
    touched_files: set[str],
    touched_dirs: set[str],
) -> list[str]:
    learned = summary.get("what_was_learned")
    if not isinstance(learned, list):
        return []
    claims: list[str] = []
    for item in learned:
        if not isinstance(item, Mapping):
            continue
        if not _claim_has_dev_relevant_evidence(
            item.get("evidence"),
            touched_files=touched_files,
            touched_dirs=touched_dirs,
        ):
            continue
        claim = _text(item.get("claim"), limit=_MAX_FIELD_CHARS)
        if claim:
            claims.append(claim)
    return claims


def _claim_has_dev_relevant_evidence(
    evidence: Any,
    *,
    touched_files: set[str],
    touched_dirs: set[str],
) -> bool:
    if not isinstance(evidence, list) or not evidence:
        return False
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        evidence_type = _text(item.get("type"))
        if evidence_type in {"review_finding", "plan_step", "diff"}:
            return True
        if evidence_type != "file":
            continue
        path = _text(item.get("path"))
        if not path:
            continue
        if path in touched_files or str(Path(path).parent) in touched_dirs:
            return True
    return False


def _finding_metadata(value: Any) -> tuple[int, list[str]]:
    if not isinstance(value, list):
        return (0, [])
    total = sum(1 for item in value if isinstance(item, Mapping))
    metadata: list[str] = []
    for item in value[:_MAX_RENDERED_CLAIMS]:
        if not isinstance(item, Mapping):
            continue
        parts = []
        finding_id = _text(item.get("finding_id"))
        if finding_id:
            parts.append(f"id={finding_id}")
        cycles_seen = item.get("cycles_seen")
        if cycles_seen is not None:
            parts.append(f"cycles_seen={cycles_seen}")
        if parts:
            metadata.append(", ".join(parts))
    return total, metadata


def _finding_descriptions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    findings: list[str] = []
    for item in value[:_MAX_RENDERED_CLAIMS]:
        if not isinstance(item, Mapping):
            continue
        finding_id = _text(item.get("finding_id"))
        description = _text(item.get("description"), limit=_MAX_FIELD_CHARS)
        cycles_seen = item.get("cycles_seen")
        resolution = _text(item.get("resolution"), limit=_MAX_FIELD_CHARS)
        detail = description or resolution
        if not detail:
            continue
        prefix = f"{finding_id}: " if finding_id else ""
        suffix = f" (cycles_seen={cycles_seen})" if cycles_seen is not None else ""
        findings.append(f"{prefix}{detail}{suffix}")
    return findings


def _rendering_mode_for_phase(phase: str) -> str:
    if phase in SIGNAL_ONLY_PHASES:
        return "signal_only"
    if phase in ELIGIBLE_PHASES:
        return "phase_summary"
    return ""


def _measure_rendered_summary(content: str) -> RenderedSummarySize:
    """Measure the rendered summary text alone, not any wider prompt assembly."""
    if not content:
        return RenderedSummarySize(value=0)
    try:
        return RenderedSummarySize(value=_estimated_token_count(content))
    except Exception:  # noqa: BLE001 - telemetry must degrade to unavailable, never fail selection
        return RenderedSummarySize(value=None, unavailable_reason="measurement_failed")


def _estimated_token_count(content: str) -> int:
    """Return a deterministic local estimate of prompt token contribution.

    The selector runs on the prompt-assembly path, so counting must not depend
    on optional packages or any first-use cache/network behavior.
    """
    return len(re.findall(r"\w+|[^\w\s]", content, flags=re.UNICODE))


# ── Reason strings ────────────────────────────────────────────────────────────


def _inadmissible_reason(verdict: KnowledgeSummaryVerdict) -> str:
    detail = ", ".join(verdict.reasons) if verdict.reasons else "unspecified"
    return f"inadmissible({detail})"


def _stale_reason(verdict: KnowledgeSummaryVerdict) -> str:
    detail = ", ".join(verdict.reasons) if verdict.reasons else "reduced_rank"
    return f"stale({detail})"


# ── Small helpers ─────────────────────────────────────────────────────────────


def _text(value: Any, *, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit] if limit is not None else text


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if isinstance(item, str) and _text(item)]


def _capped(values: list[str]) -> list[str]:
    return values[:_MAX_REASON_DETAILS]


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_]+", (text or "").lower())
        if len(token) > 1 and token not in _NOISE_TOKENS
    }
