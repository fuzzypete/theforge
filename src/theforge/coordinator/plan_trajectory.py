"""Plan trajectory tracking: per-attempt metadata and convergence detection.

Provides theme extraction from plan review findings, disposition classification
(patch / backtrack / escalate), and context generation for regen prompts.
No LLM calls — all logic is deterministic regex + set operations.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from theforge.review import PlanReviewFinding

    from .state import CoordinatorState


# ── Regex patterns for structural anchors ─────────────────────────────

# Multi-segment snake_case with optional leading underscore: load_config, _validate_plan_provider
_RE_SNAKE = re.compile(r"\b_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# Multi-segment camelCase: loadConfig, checkAgentAuth
_RE_CAMEL = re.compile(r"\b[a-z]+[A-Z][a-zA-Z0-9]*\b")

# Dotted paths with 2+ segments: config.profiles.dev, state.plan_attempt_metadata
_RE_DOTTED = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+\b")

# File paths with known code extensions
_CODE_EXTENSIONS = frozenset(
    {"py", "ts", "js", "go", "rs", "java", "rb", "c", "cpp", "h", "yaml", "yml", "toml", "json"}
)
_RE_FILE_PATH = re.compile(r"\b(?:[\w./]+/)?[\w.-]+\.(\w{1,4})\b")


def _is_code_file_path(s: str) -> bool:
    """Return True if s looks like a file path with a known code extension."""
    m = _RE_FILE_PATH.fullmatch(s)
    return bool(m and m.group(1).lower() in _CODE_EXTENSIONS)


def _is_file_path_anchor(theme: str) -> bool:
    """Return True if theme is a file path with a known code extension.

    File paths like coordinator.py or src/theforge/config.py are file-path anchors.
    Pure identifiers (load_config) and dotted paths without code extensions
    (config.profiles.dev) are not.
    """
    return _is_code_file_path(theme)


def extract_finding_themes(findings: Iterable["PlanReviewFinding"]) -> list[str]:
    """Extract deduped structural anchor themes from plan review finding descriptions.

    Applies regex patterns to each finding's description text to identify:
    - Multi-segment snake_case identifiers (load_config, _validate_plan_provider)
    - Multi-segment camelCase identifiers (loadConfig, checkAgentAuth)
    - Dotted paths with 2+ segments (config.profiles.dev)
    - File paths with known code extensions (coordinator.py, src/theforge/config.py)

    Returns sorted, deduplicated list. No LLM call.
    """
    seen: set[str] = set()
    for finding in findings:
        text = finding.description or ""
        for pattern in (_RE_SNAKE, _RE_CAMEL, _RE_DOTTED):
            for m in pattern.finditer(text):
                seen.add(m.group(0))
        for m in _RE_FILE_PATH.finditer(text):
            match_str = m.group(0)
            if m.group(1).lower() in _CODE_EXTENSIONS:
                seen.add(match_str)
    return sorted(seen)


def _has_sufficient_overlap(prev_themes: list[str], curr_themes: list[str]) -> bool:
    """Return True if there is at least one shared theme that is NOT a file-path-only anchor.

    File-path-only overlap is insufficient per the structural anchor model — at least
    one surviving identifier or dotted path must be shared.
    """
    prev_set = set(prev_themes)
    curr_set = set(curr_themes)
    surviving = prev_set & curr_set
    return any(not _is_file_path_anchor(t) for t in surviving)


def classify_disposition(metadata_history: list[dict]) -> str:
    """Classify the latest plan attempt as 'patch', 'backtrack', or 'escalate'.

    Args:
        metadata_history: Full list of plan_attempt_metadata dicts. Each entry has
            files_touched (int), p1_count (int), p2_count (int), finding_themes (list[str]).

    Returns:
        'patch'     — no sufficient theme overlap OR finding count is decreasing
        'backtrack' — sufficient overlap AND files_touched flat or growing
        'escalate'  — previous disposition was 'backtrack' AND sufficient overlap still holds

    Ordering: escalate is checked before backtrack (escalate is a stricter subset).
    """
    if len(metadata_history) < 2:
        return "patch"

    prev = metadata_history[-2]
    curr = metadata_history[-1]

    prev_themes: list[str] = prev.get("finding_themes", [])
    curr_themes: list[str] = curr.get("finding_themes", [])

    prev_count = prev.get("p1_count", 0) + prev.get("p2_count", 0)
    curr_count = curr.get("p1_count", 0) + curr.get("p2_count", 0)

    has_sufficient = _has_sufficient_overlap(prev_themes, curr_themes)
    complexity_flat_or_growing = curr.get("files_touched", 0) >= prev.get("files_touched", 0)

    # patch: no meaningful overlap OR finding count is decreasing OR complexity shrank
    if not has_sufficient or curr_count < prev_count or not complexity_flat_or_growing:
        return "patch"

    # sufficient overlap + flat/growing complexity: check escalate before backtrack
    if len(metadata_history) >= 3:
        prev_disposition = classify_disposition(metadata_history[:-1])
        if prev_disposition == "backtrack":
            return "escalate"

    return "backtrack"


def record_plan_attempt(
    state: "CoordinatorState",
    findings: "Iterable[PlanReviewFinding]",
) -> None:
    """Append per-attempt metadata to state and update plan_regen_disposition.

    Extracts files_touched from state.plan_structured, counts P1/P2 findings,
    extracts finding themes, appends the metadata dict, and computes disposition.
    """
    # Count unique files proposed in the plan
    files_touched = len(
        {
            f
            for step in (state.plan_structured or {}).get("steps", [])
            for f in step.get("files", [])
        }
    )

    findings_list = list(findings)
    p1_count = sum(1 for f in findings_list if f.severity in ("P0", "P1"))
    p2_count = sum(1 for f in findings_list if f.severity == "P2")
    finding_themes = extract_finding_themes(findings_list)

    state.plan_attempt_metadata.append(
        {
            "files_touched": files_touched,
            "p1_count": p1_count,
            "p2_count": p2_count,
            "finding_themes": finding_themes,
        }
    )
    state.plan_regen_disposition = classify_disposition(state.plan_attempt_metadata)


def collect_all_surviving_themes(metadata: list[dict]) -> list[str]:
    """Return deduped sorted list of non-file-path themes across ALL attempts."""
    all_themes: set[str] = set()
    for entry in metadata:
        for theme in entry.get("finding_themes", []):
            if not _is_file_path_anchor(theme):
                all_themes.add(theme)
    return sorted(all_themes)


def dominant_surviving_theme(metadata: list[dict]) -> str:
    """Return the non-file-path theme from the latest attempt pair with the most
    historical consecutive-pair survivals.

    Only themes that survive in the latest (most recent) consecutive pair are
    candidates, ensuring the "rejected strategy" refers to what is actively
    failing in the current attempt rather than a resolved older theme.  Among
    qualifying candidates, the theme with the highest total pair-survival count
    is returned. Alphabetically smallest breaks ties for determinism.
    Returns empty string if no qualifying theme exists.
    """
    if len(metadata) < 2:
        return ""

    # Restrict candidates to themes surviving in the latest pair only.
    latest_prev = set(metadata[-2].get("finding_themes", []))
    latest_curr = set(metadata[-1].get("finding_themes", []))
    candidates = {t for t in latest_prev & latest_curr if not _is_file_path_anchor(t)}

    if not candidates:
        return ""

    # Score candidates by total historical pair-survival count.
    pair_counts: dict[str, int] = {}
    for i in range(1, len(metadata)):
        prev_set = set(metadata[i - 1].get("finding_themes", []))
        curr_set = set(metadata[i].get("finding_themes", []))
        for theme in prev_set & curr_set:
            if theme in candidates:
                pair_counts[theme] = pair_counts.get(theme, 0) + 1

    if not pair_counts:
        return ""

    max_count = max(pair_counts.values())
    # Alphabetically smallest among tied winners for determinism
    return min(t for t, c in pair_counts.items() if c == max_count)


def build_disposition_context(state: "CoordinatorState") -> str:
    """Build disposition-specific prompt content for injection into regen prompts.

    Returns:
    - Empty string if fewer than 2 attempts (no trajectory to compare).
    - Empty string if disposition is "escalate" (coordinator handles escalation,
      no prompt is issued).
    - Trajectory table + patch guidance if disposition is "patch".
    - Backtrack block with learned constraints + rejected strategy if disposition
      is "backtrack". This function is the single owner of all backtrack-specific
      prompt text.
    """
    metadata = state.plan_attempt_metadata
    if len(metadata) < 2:
        return ""

    disposition = state.plan_regen_disposition or "patch"

    if disposition == "escalate":
        return ""

    if disposition == "backtrack":
        n = len(metadata)
        themes = collect_all_surviving_themes(metadata)
        constraints_text = "\n".join(f"- {t}" for t in themes) if themes else "- (none identified)"

        dom_theme = dominant_surviving_theme(metadata)
        if dom_theme:
            # Count how many consecutive pairs this theme survived
            pair_counts: dict[str, int] = {}
            for i in range(1, len(metadata)):
                prev_set = set(metadata[i - 1].get("finding_themes", []))
                curr_set = set(metadata[i].get("finding_themes", []))
                for t in prev_set & curr_set:
                    if not _is_file_path_anchor(t):
                        pair_counts[t] = pair_counts.get(t, 0) + 1
            m = pair_counts.get(dom_theme, 0)
            rejected_text = (
                f"- threading `{dom_theme}` — this approach has been flagged"
                f" in {m} consecutive reviews."
            )
        else:
            rejected_text = "- (none identified)"

        return (
            f"## Trajectory Analysis\n\n"
            f"Your plan has been rejected {n} times."
            f" The current approach is not converging.\n\n"
            f"### Learned constraints\n\n"
            f"Validated truths from reviewer findings across all attempts:\n\n"
            f"{constraints_text}\n\n"
            f"### Rejected strategy\n\n"
            f"{rejected_text}\n\n"
            f"Do not patch the current plan. Do not repeat the rejected strategy."
            f" Produce a new plan that satisfies the learned constraints using a"
            f" different approach. Prefer the smallest design that works."
        )

    # patch: trajectory table + guidance
    guidance = "Prior themes were resolved. Focus on the new findings only."

    # Build table — show surviving themes for each attempt
    header = "| Attempt | Files Touched | P1 | P2 | Surviving Themes |"
    separator = "|---------|--------------|----|----|-----------------|"
    rows: list[str] = []
    for i, entry in enumerate(metadata, start=1):
        if i == 1:
            surviving = ", ".join(entry.get("finding_themes", [])) or "—"
        else:
            prev_set = set(metadata[i - 2].get("finding_themes", []))
            curr_set = set(entry.get("finding_themes", []))
            surviving_themes = sorted(prev_set & curr_set)
            surviving = ", ".join(surviving_themes) or "—"
        rows.append(
            f"| {i} | {entry.get('files_touched', 0)} "
            f"| {entry.get('p1_count', 0)} "
            f"| {entry.get('p2_count', 0)} "
            f"| {surviving} |"
        )

    table = "\n".join([header, separator] + rows)

    return f"## Trajectory Analysis\n\nDisposition: **{disposition}**\n\n{table}\n\n{guidance}"
