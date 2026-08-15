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
from theforge.knowledge_index import KNOWLEDGE_INDEX_PATH, KNOWLEDGE_INDEX_SCHEMA_VERSION

#: ``ContextItem.kind`` for an injected prior-run summary. Budget accounting and
#: the audit manifest both branch on this value.
PRIOR_RUN_KIND = "prior_run_summary"

#: ADR-0002 clause 5: summary prose may advise the planning, development, and
#: review agents. It must never reach preflight, whose output (sufficiency,
#: complexity, likely files, refusal) drives coordinator control flow.
ELIGIBLE_PHASES = frozenset({"plan", "dev", "review"})

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


@dataclass(frozen=True)
class PriorRunCandidate:
    """One prior summary that is eligible to be offered to an agent."""

    run_id: str
    summary_path: str
    score: int
    reason: str
    verdict: KnowledgeSummaryVerdict
    content: str

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
    phase_eligible: bool = True


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
    if normalized_phase not in ELIGIBLE_PHASES:
        return PriorRunSelection(phase_eligible=False)

    entries = _load_index_entries(Path(project_root))
    if not entries:
        return PriorRunSelection(entry_count=0)

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

        content = _render_summary(Path(project_root), entry, run_id=run_id, verdict=verdict)
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
        phase_eligible=True,
    )


# ── Index loading ─────────────────────────────────────────────────────────────


def _load_index_entries(project_root: Path) -> list[Mapping[str, Any]]:
    """Read the deterministic index, failing closed to ``[]`` on any problem."""
    path = project_root / KNOWLEDGE_INDEX_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(raw, Mapping):
        return []
    if raw.get("schema_version") != KNOWLEDGE_INDEX_SCHEMA_VERSION:
        return []
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, Mapping)]


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
) -> str:
    """Render bounded advisory prose from the referenced summary artifact."""
    rel_path = _text(entry.get("summary_path"))
    if not rel_path:
        return ""
    try:
        summary = yaml.safe_load((project_root / rel_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return ""
    if not isinstance(summary, Mapping):
        return ""

    lines = [
        f"## Prior run {run_id} (advisory — {verdict.status})",
        "",
        (
            "A previous run on related code produced the notes below. They are "
            "advisory context, not requirements, and may be out of date."
        ),
        "",
    ]

    what_changed = summary.get("what_changed")
    if isinstance(what_changed, Mapping):
        description = _text(what_changed.get("description"), limit=_MAX_FIELD_CHARS)
        approach = _text(what_changed.get("approach"), limit=_MAX_FIELD_CHARS)
        if description:
            lines.append(f"- What changed: {description}")
        if approach:
            lines.append(f"- Approach: {approach}")

    learned = summary.get("what_was_learned")
    if isinstance(learned, list):
        claims = [
            _text(item.get("claim"), limit=_MAX_FIELD_CHARS)
            for item in learned
            if isinstance(item, Mapping)
        ]
        claims = [claim for claim in claims if claim][:_MAX_RENDERED_CLAIMS]
        if claims:
            lines.append("- Learned:")
            lines.extend(f"  - {claim}" for claim in claims)

    patterns = _string_list(entry.get("learned_patterns"))[:_MAX_RENDERED_PATTERNS]
    if patterns:
        lines.append(f"- Patterns: {', '.join(patterns)}")

    if verdict.reasons:
        lines.append(f"- Verdict caveats: {', '.join(verdict.reasons)}")

    return "\n".join(lines).strip()


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
