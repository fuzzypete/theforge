"""Which project invariants a phase may see, and how narrowly (#1875).

The consumer half of the invariant-index spike. :mod:`theforge.invariant_index`
materializes provenance and applicability metadata over marked regions of the
project's own Markdown; this module decides which of those regions reach a
prompt, and — the part the spike actually exists to test — *how much of the
source document comes with them*.

Three rules shape everything here:

1. **Uncertainty widens, never narrows.** A rule whose declared scope cannot be
   matched against the story's files with confidence is included as its whole
   enclosing source section, not dropped and not trimmed to a guess. The only
   drop that narrowing may cause is the confident one: the project declared file
   globs, the touched files are known, and none of them match.
2. **Review is deliberately broader than plan/dev.** Plan and dev may receive
   narrow capsules; review always receives the enclosing source section. A bad
   scope decision then reaches the producer without also blinding the reviewer,
   which is the correlated-miss failure this asymmetry exists to prevent.
3. **The source document is authoritative; the index is not.** Text is always
   re-read from the source file at render time. The indexed digest is compared
   only to *report* staleness, never to suppress a rule.

Preflight is excluded outright, on the same ground as prior-run summaries:
preflight's output (sufficiency, complexity, likely files, refusal) drives
coordinator control flow, so no marked prose may reach it (ADR-0002 clause 5).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from theforge.invariant_index import (
    COMPLETENESS_FULL,
    COMPLETENESS_NONE,
    COMPLETENESS_PARTIAL,
    load_invariant_index,
)

#: ``ContextItem.kind`` for an injected project invariant. Budget accounting and
#: the audit manifest both branch on this value.
INVARIANT_KIND = "project_invariant"

#: Phases that may receive invariant prose at all.
ELIGIBLE_PHASES = frozenset({"plan", "dev", "review"})

#: Phases that always receive the broader source section, never a capsule.
BROAD_SOURCE_PHASES = frozenset({"review"})

RENDER_CAPSULE = "capsule"
RENDER_SOURCE_SECTION = "source_section"

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

#: Lines of enclosing source a single broad inclusion may render before it is
#: truncated with a pointer back to the file. Broad means "the section", not
#: "the repository".
_MAX_SECTION_LINES = 120

_SCORE_BASE = 100
_SCORE_ENFORCEMENT = {"gate": 15, "review": 8, "advisory": 0}
_SCORE_CONFIDENT = 5

_HEADING_PATTERN = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")


@dataclass(frozen=True)
class InvariantCandidate:
    """One invariant offered to a phase, already rendered from its source."""

    invariant_id: str
    source_path: str
    source_anchor: str
    enforcement: str
    rendering_mode: str
    scope_confidence: str
    reason: str
    content: str
    score: int
    source_digest_matches: bool

    @property
    def source(self) -> str:
        """Human-readable provenance label for the context manifest.

        Display only. Identity travels separately as ``ContextItem.item_id`` —
        source paths are project-controlled and may contain the delimiter, so
        this string is not parseable back into a path and an id.
        """
        return f"{self.source_path}#{self.invariant_id}"


@dataclass(frozen=True)
class InvariantExclusion:
    invariant_id: str
    source_path: str
    reason: str


@dataclass(frozen=True)
class InvariantSelection:
    phase: str
    phase_eligible: bool
    entry_count: int
    selection_mode: str
    candidates: tuple[InvariantCandidate, ...] = ()
    excluded: tuple[InvariantExclusion, ...] = ()

    @property
    def uncertain(self) -> tuple[InvariantCandidate, ...]:
        """Candidates included broadly *because* scope confidence was low."""
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.scope_confidence == CONFIDENCE_LOW
        )


def select_invariants(
    project_root: Path,
    *,
    phase: str,
    story_text: str,
    file_list: list[str] | None,
) -> InvariantSelection:
    """Choose the invariants this phase may see. Never raises."""
    normalized_phase = phase.lower()
    if normalized_phase not in ELIGIBLE_PHASES:
        return InvariantSelection(
            phase=normalized_phase,
            phase_eligible=False,
            entry_count=0,
            selection_mode="",
        )

    entries = load_invariant_index(project_root)
    broad_phase = normalized_phase in BROAD_SOURCE_PHASES
    selection_mode = RENDER_SOURCE_SECTION if broad_phase else "selective"

    candidates: list[InvariantCandidate] = []
    excluded: list[InvariantExclusion] = []
    sources = _SourceCache(project_root)

    for entry in entries:
        invariant_id = str(entry.get("id", ""))
        source_path = str(entry.get("source_path", ""))
        applicability = entry.get("applicability")
        applicability = applicability if isinstance(applicability, dict) else {}
        phases = _string_list(applicability.get("phases"))
        if phases and normalized_phase not in phases:
            excluded.append(
                InvariantExclusion(
                    invariant_id,
                    source_path,
                    f"phase_not_applicable({','.join(phases)})",
                )
            )
            continue

        confidence, reason, applies = _scope_confidence(
            applicability, file_list=file_list, story_text=story_text
        )
        if not applies:
            if not broad_phase:
                excluded.append(InvariantExclusion(invariant_id, source_path, reason))
                continue
            # Review is broader on purpose: the one narrowing decision plan/dev
            # is allowed to make must not also blind the reviewer, or a bad
            # scope tag produces a correlated miss on both sides.
            reason = f"broad_phase_override({reason})"

        rendered = sources.render(entry, broad=broad_phase or confidence == CONFIDENCE_LOW)
        if rendered is None:
            excluded.append(InvariantExclusion(invariant_id, source_path, "source_unreadable"))
            continue
        content, mode, digest_matches = rendered

        candidates.append(
            InvariantCandidate(
                invariant_id=invariant_id,
                source_path=source_path,
                source_anchor=str(entry.get("source_anchor") or ""),
                enforcement=str(entry.get("enforcement") or "advisory"),
                rendering_mode=mode,
                scope_confidence=confidence,
                reason=reason,
                content=content,
                score=_score(entry, confidence),
                source_digest_matches=digest_matches,
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.source_path, item.invariant_id))
    return InvariantSelection(
        phase=normalized_phase,
        phase_eligible=True,
        entry_count=len(entries),
        selection_mode=selection_mode,
        candidates=tuple(candidates),
        excluded=tuple(excluded),
    )


# ── Scope confidence ─────────────────────────────────────────────────────────


def _scope_confidence(
    applicability: dict,
    *,
    file_list: list[str] | None,
    story_text: str,
) -> tuple[str, str, bool]:
    """Decide confidence, the reason for it, and whether the rule applies.

    The concrete triggers, so no consumer has to infer them:

    - ``full`` scope (file globs declared) **and** a known file list: a glob
      match is high confidence; no match is the one confident *drop*.
    - ``full`` scope with **no** known file list: low confidence — there is
      nothing to match against, so the rule widens rather than guessing.
    - ``partial`` scope (areas only): an area token appearing in a touched path
      or in the story text is high confidence; otherwise low.
    - ``none`` scope, or scope tokens the extractor could not parse: always low.
    """
    completeness = str(applicability.get("scope_completeness") or COMPLETENESS_NONE)
    unparsed = _string_list(applicability.get("unparsed_scope_keys"))
    globs = _string_list(applicability.get("file_globs"))
    areas = _string_list(applicability.get("areas"))
    files = [item for item in (file_list or []) if isinstance(item, str) and item.strip()]

    if unparsed:
        return (CONFIDENCE_LOW, f"unparsed_scope({','.join(unparsed)})", True)

    if completeness == COMPLETENESS_FULL and globs:
        if not files:
            return (CONFIDENCE_LOW, "no_file_list_to_match_scope", True)
        matched = [glob for glob in globs if _any_match(files, glob)]
        if matched:
            return (CONFIDENCE_HIGH, f"file_scope_match({','.join(sorted(matched)[:3])})", True)
        return (CONFIDENCE_HIGH, f"files_out_of_scope({','.join(globs[:3])})", False)

    if completeness == COMPLETENESS_PARTIAL and areas:
        haystack = " ".join(files).lower() + " " + story_text.lower()
        matched = [area for area in areas if area.lower() in haystack]
        if matched:
            return (CONFIDENCE_HIGH, f"area_match({','.join(sorted(matched)[:3])})", True)
        return (CONFIDENCE_LOW, f"area_unmatched({','.join(areas[:3])})", True)

    return (CONFIDENCE_LOW, "no_scope_metadata", True)


def _any_match(files: list[str], glob: str) -> bool:
    normalized = glob.strip()
    for path in files:
        candidate = path.strip().lstrip("./")
        if fnmatch.fnmatch(candidate, normalized) or fnmatch.fnmatch(
            candidate, f"*/{normalized.lstrip('/')}"
        ):
            return True
        if normalized.endswith("/") and candidate.startswith(normalized):
            return True
    return False


def _score(entry: dict, confidence: str) -> int:
    enforcement = str(entry.get("enforcement") or "advisory")
    score = _SCORE_BASE + _SCORE_ENFORCEMENT.get(enforcement, 0)
    if confidence == CONFIDENCE_HIGH:
        score += _SCORE_CONFIDENT
    return score


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


# ── Rendering from the authoritative source ──────────────────────────────────


@dataclass
class _SourceCache:
    """Re-reads source documents once per assembly. The index never holds prose."""

    project_root: Path
    _texts: dict[str, list[str] | None] = field(default_factory=dict)

    def lines(self, source_path: str) -> list[str] | None:
        if source_path not in self._texts:
            try:
                text = (self.project_root / source_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._texts[source_path] = None
            else:
                self._texts[source_path] = text.splitlines()
        return self._texts[source_path]

    def render(self, entry: dict, *, broad: bool) -> tuple[str, str, bool] | None:
        source_path = str(entry.get("source_path", ""))
        lines = self.lines(source_path)
        if not lines:
            return None
        body = _slice(lines, entry.get("body_start_line"), entry.get("body_end_line"))
        if not body.strip():
            return None
        digest_matches = _digest_matches(body, entry.get("source_digest"))
        header_scope = str(entry.get("scope_raw") or "unscoped")
        enforcement = str(entry.get("enforcement") or "advisory")

        if not broad:
            header = (
                f"### Project invariant `{entry.get('id')}` "
                f"({source_path}:{entry.get('start_line')}, enforcement: {enforcement})"
            )
            return (f"{header}\n\n{body.strip()}", RENDER_CAPSULE, digest_matches)

        section, start_line = _enclosing_section(lines, entry)
        header = (
            f"### Project invariant source `{entry.get('id')}` "
            f"({source_path}:{start_line}, enforcement: {enforcement}, scope: {header_scope})\n"
            "Included as the full enclosing section rather than a narrowed excerpt."
        )
        return (f"{header}\n\n{section.strip()}", RENDER_SOURCE_SECTION, digest_matches)


def _slice(lines: list[str], start: object, end: object) -> str:
    if not isinstance(start, int) or not isinstance(end, int):
        return ""
    first = max(start - 1, 0)
    last = min(end, len(lines))
    if first >= last:
        return ""
    return "\n".join(lines[first:last])


def _enclosing_section(lines: list[str], entry: dict) -> tuple[str, int]:
    """The Markdown section containing the marker, truncated to a bounded size."""
    marker_line = entry.get("start_line")
    marker_index = (marker_line - 1) if isinstance(marker_line, int) else 0
    marker_index = max(0, min(marker_index, len(lines) - 1))

    start = 0
    level = 1
    for index in range(marker_index, -1, -1):
        match = _HEADING_PATTERN.match(lines[index])
        if match:
            start = index
            level = len(match.group("hashes"))
            break

    end = len(lines)
    for index in range(marker_index + 1, len(lines)):
        match = _HEADING_PATTERN.match(lines[index])
        if match and len(match.group("hashes")) <= level:
            end = index
            break

    section_lines = lines[start:end]
    if len(section_lines) > _MAX_SECTION_LINES:
        section_lines = section_lines[:_MAX_SECTION_LINES]
        section_lines.append(
            f"[section truncated at {_MAX_SECTION_LINES} lines — "
            f"read {entry.get('source_path')} for the rest]"
        )
    return ("\n".join(section_lines), start + 1)


def _digest_matches(body: str, recorded: object) -> bool:
    if not isinstance(recorded, str) or not recorded.startswith("sha256:"):
        return False
    import hashlib  # noqa: PLC0415

    return recorded == "sha256:" + hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
