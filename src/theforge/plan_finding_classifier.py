"""Plan-finding identity: structural anchor extraction and anchor-first matching.

Pure Python, deterministic, no LLM calls.

Two findings are considered a match if they share at least one structural anchor,
with the additional constraint that file-path-only overlap is insufficient — at
least one non-file anchor (identifier or dotted path) must also be shared.

Prose similarity (Jaccard) may break ties among candidates that already share a
structural anchor, but it is never the sole basis for a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from theforge.review import PlanReviewFinding

# ── Exclusion sets ────────────────────────────────────────────────────────────

# Common abbreviations whose dotted form looks like a path anchor but is not.
_ABBREVIATION_ROOTS: frozenset[str] = frozenset({"e.g", "i.e", "etc", "vs"})

# Plan-step label patterns: excluded before anchor extraction.
_PLAN_STEP_RE = re.compile(r"\b(?:Step|Phase)\s+\d+\b", re.IGNORECASE)

# Reviewer attribution prefix added by merge_plan_review_results (review.py).
_REVIEWER_PREFIX_RE = re.compile(r"^\[[\w-]+\]\s*")

# ── Known file extensions ─────────────────────────────────────────────────────

# File paths are matched by checking against this allowlist of extensions so
# that dotted attribute paths (e.g. ``config.profiles.dev``, ``state.init``)
# are not misclassified as filenames.
_KNOWN_EXTENSIONS: frozenset[str] = frozenset(
    {
        # Python
        "py",
        "pyi",
        "pyc",
        # Config / data
        "yaml",
        "yml",
        "json",
        "toml",
        "ini",
        "cfg",
        "env",
        # Docs
        "md",
        "txt",
        "rst",
        # Web
        "js",
        "ts",
        "jsx",
        "tsx",
        "html",
        "css",
        "scss",
        # Other languages
        "go",
        "rs",
        "rb",
        "java",
        "kt",
        "swift",
        "c",
        "cpp",
        "h",
        "cs",
        "php",
        # Shell
        "sh",
        "bash",
        "zsh",
        # Misc
        "lock",
        "log",
        # Data / database
        "sql",
        "db",
        "csv",
        "xml",
        "pdf",
        # Image / binary
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "ico",
        "gz",
        "zip",
        "out",
        # pip-tools convention (.in → compiled requirements)
        "in",
    }
)

# ── Anchor regexes ────────────────────────────────────────────────────────────

# File path: word chars and forward slashes followed by an extension (1–5 chars).
# The extension is verified against _KNOWN_EXTENSIONS after the regex match.
_FILE_PATH_RE = re.compile(r"\b[\w/]+\.\w{1,5}\b")

# Multi-segment snake_case identifier (≥2 underscore-separated segments).
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# Multi-segment camelCase identifier (≥2 humps).
_CAMEL_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b")

# Dotted path / attribute chain (≥2 dot-separated segments).
_DOTTED_RE = re.compile(r"\b\w+(?:\.\w+)+\b")


# ── Anchor dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Anchor:
    """A structural anchor extracted from a finding description."""

    value: str
    kind: Literal["file_path", "snake_ident", "camel_ident", "dotted_path"]


# ── Composite dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanFindingAnchors:
    """A plan review finding paired with its extracted anchors."""

    finding: "PlanReviewFinding"
    anchors: frozenset[Anchor]


@dataclass(frozen=True)
class MatchResult:
    """Result of matching one current finding against the prior finding set."""

    current_index: int
    prior_index: int | None  # None = unmatched / abstain
    shared_anchors: tuple[Anchor, ...]
    prose_similarity_used: bool
    abstain_reason: str | None


# ── Helpers ───────────────────────────────────────────────────────────────────


def strip_reviewer_prefix(description: str) -> str:
    """Strip leading ``[reviewer-name]`` attribution prefix from a description.

    merge_plan_review_results() in review.py prefixes every merged finding
    with ``[name] `` for attribution.  Stripping this before anchor extraction
    prevents reviewer names (which can contain hyphens) from becoming anchors.
    """
    return _REVIEWER_PREFIX_RE.sub("", description)


def _is_version_like(value: str) -> bool:
    """Return True if value looks like a version number (e.g. ``3.12.1``)."""
    parts = value.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts[1:])


def _is_abbreviation(value: str) -> bool:
    """Return True if value is a known common abbreviation."""
    return value.lower().rstrip(".") in _ABBREVIATION_ROOTS


def _has_single_char_non_first_segment(value: str) -> bool:
    """Return True if any segment after the first is a single character."""
    return any(len(p) == 1 for p in value.split(".")[1:])


def _jaccard_str(a: str, b: str) -> float:
    """Compute Jaccard token overlap between two strings (case-insensitive)."""
    tokens_a = set(re.findall(r"\w+", a.lower()))
    tokens_b = set(re.findall(r"\w+", b.lower()))
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ── Core extraction ───────────────────────────────────────────────────────────


def extract_anchors(text: str) -> frozenset[Anchor]:
    """Extract structural anchors from ``text``.

    Processing order:
    1. Strip plan-step labels (``Step N``, ``Phase N``).
    2. Extract file paths (word chars + ``/`` with a short extension).
    3. Extract dotted paths that do not overlap a file-path span.
    4. Extract multi-segment snake_case identifiers not inside a file-path span.
    5. Extract multi-segment camelCase identifiers not inside a file-path span.

    Single-segment words, bare numbers, version strings, and common abbreviations
    are excluded.  The ``suggestion`` field is intentionally NOT passed here
    (per spec: anchors come from ``description`` only).
    """
    clean = _PLAN_STEP_RE.sub("", text)

    anchors: set[Anchor] = set()
    fp_spans: list[tuple[int, int]] = []

    # ── 1. File paths ──────────────────────────────────────────────────────
    for m in _FILE_PATH_RE.finditer(clean):
        val = m.group()
        ext = val.rsplit(".", 1)[-1].lower()
        if ext not in _KNOWN_EXTENSIONS:
            continue
        if _is_abbreviation(val) or _is_version_like(val):
            continue
        anchors.add(Anchor(value=val, kind="file_path"))
        fp_spans.append(m.span())

    def _overlaps_file_path(start: int, end: int) -> bool:
        return any(start < fe and end > fs for fs, fe in fp_spans)

    # ── 2. Dotted paths ────────────────────────────────────────────────────
    for m in _DOTTED_RE.finditer(clean):
        val = m.group()
        start, end = m.span()
        if _overlaps_file_path(start, end):
            continue
        if _is_abbreviation(val) or _is_version_like(val):
            continue
        if _has_single_char_non_first_segment(val):
            continue
        anchors.add(Anchor(value=val, kind="dotted_path"))

    # ── 3. Multi-segment snake_case ────────────────────────────────────────
    for m in _SNAKE_RE.finditer(clean):
        val = m.group()
        start, end = m.span()
        if _overlaps_file_path(start, end):
            continue
        anchors.add(Anchor(value=val, kind="snake_ident"))

    # ── 4. Multi-segment camelCase ─────────────────────────────────────────
    for m in _CAMEL_RE.finditer(clean):
        val = m.group()
        start, end = m.span()
        if _overlaps_file_path(start, end):
            continue
        anchors.add(Anchor(value=val, kind="camel_ident"))

    return frozenset(anchors)


# ── Matching ──────────────────────────────────────────────────────────────────


def match_plan_findings(
    current: list[PlanReviewFinding],
    prior: list[PlanReviewFinding],
) -> list[MatchResult]:
    """Match each current finding against prior findings using anchor-first logic.

    Rules:
    - A match requires ≥1 shared anchor.
    - File-path-only overlap is insufficient: at least one non-file anchor must
      also be shared.
    - Non-file anchors (snake_ident, camel_ident, dotted_path) are sufficient
      on their own.
    - When multiple prior candidates qualify, Jaccard token overlap on stripped
      descriptions is used as a tie-breaker (``prose_similarity_used=True``).
    - When no prior candidate qualifies, the finding is unmatched (abstain).
    - Reviewer attribution prefixes are stripped before anchor extraction.
    - Plan-step labels are stripped inside ``extract_anchors``.

    Returns one ``MatchResult`` per current finding (in order).
    """
    if not prior:
        return [
            MatchResult(
                current_index=i,
                prior_index=None,
                shared_anchors=(),
                prose_similarity_used=False,
                abstain_reason="no prior findings to match against",
            )
            for i in range(len(current))
        ]

    # Pre-extract anchors for all findings (strip prefix first).
    current_clean = [strip_reviewer_prefix(f.description) for f in current]
    prior_clean = [strip_reviewer_prefix(f.description) for f in prior]
    current_anchors = [extract_anchors(desc) for desc in current_clean]
    prior_anchors = [extract_anchors(desc) for desc in prior_clean]

    results: list[MatchResult] = []
    used_prior_indices: set[int] = set()

    for ci in range(len(current)):
        ca = current_anchors[ci]

        # Collect all prior candidates with qualifying anchor overlap.
        # Track already-claimed priors separately so we can emit accurate provenance.
        candidates: list[tuple[int, frozenset[Anchor]]] = []
        file_only_priors: list[int] = []
        claimed_priors_with_overlap: list[int] = []

        for pi in range(len(prior)):
            pa = prior_anchors[pi]
            shared: frozenset[Anchor] = ca & pa
            if not shared:
                continue

            non_file_shared = frozenset(a for a in shared if a.kind != "file_path")
            file_shared = frozenset(a for a in shared if a.kind == "file_path")

            if not non_file_shared and file_shared:
                # File-path-only overlap — insufficient for a match.
                if pi not in used_prior_indices:
                    file_only_priors.append(pi)
                continue

            if pi in used_prior_indices:
                # Qualifying overlap, but prior already claimed by earlier finding.
                claimed_priors_with_overlap.append(pi)
                continue

            candidates.append((pi, shared))

        if not candidates:
            if not ca:
                abstain_reason = "no extractable structural anchors in current finding"
            elif claimed_priors_with_overlap:
                abstain_reason = "prior already claimed by another finding"
            elif file_only_priors:
                abstain_reason = "file-path-only overlap — no shared non-file anchor"
            else:
                abstain_reason = "no shared structural anchors"

            results.append(
                MatchResult(
                    current_index=ci,
                    prior_index=None,
                    shared_anchors=(),
                    prose_similarity_used=False,
                    abstain_reason=abstain_reason,
                )
            )
            continue

        if len(candidates) == 1:
            pi, shared = candidates[0]
            used_prior_indices.add(pi)
            results.append(
                MatchResult(
                    current_index=ci,
                    prior_index=pi,
                    shared_anchors=tuple(sorted(shared, key=lambda a: (a.kind, a.value))),
                    prose_similarity_used=False,
                    abstain_reason=None,
                )
            )
        else:
            # Tie-break: highest Jaccard similarity between stripped descriptions.
            best_pi, best_shared = max(
                candidates,
                key=lambda c: _jaccard_str(current_clean[ci], prior_clean[c[0]]),
            )
            used_prior_indices.add(best_pi)
            results.append(
                MatchResult(
                    current_index=ci,
                    prior_index=best_pi,
                    shared_anchors=tuple(sorted(best_shared, key=lambda a: (a.kind, a.value))),
                    prose_similarity_used=True,
                    abstain_reason=None,
                )
            )

    return results


# ── Provenance formatting ─────────────────────────────────────────────────────


def format_provenance(results: list[MatchResult]) -> str:
    """Render human-readable provenance for all match/abstain decisions.

    Format (one line per finding):
    - Match:   ``finding[N] → prior[M]: matched on [kind:value, ...]{prose note}``
    - Abstain: ``finding[N]: UNMATCHED — <reason>``
    """
    lines: list[str] = []
    for r in results:
        if r.prior_index is None:
            lines.append(f"  finding[{r.current_index}]: UNMATCHED — {r.abstain_reason}")
        else:
            anchor_str = ", ".join(f"{a.kind}:{a.value}" for a in r.shared_anchors)
            prose_note = (
                " (prose similarity used for tie-breaking)" if r.prose_similarity_used else ""
            )
            lines.append(
                f"  finding[{r.current_index}] → prior[{r.prior_index}]:"
                f" matched on [{anchor_str}]{prose_note}"
            )
    return "\n".join(lines)
