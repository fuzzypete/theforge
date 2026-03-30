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


def build_disposition_context(state: "CoordinatorState") -> str:
    """Build a markdown trajectory block for injection into regen prompts.

    Returns empty string if fewer than 2 attempts have been recorded (no
    trajectory to compare). For 2+ entries renders a table and disposition
    guidance so the planning agent can adjust its approach.
    """
    metadata = state.plan_attempt_metadata
    if len(metadata) < 2:
        return ""

    disposition = state.plan_regen_disposition or "patch"

    guidance_map = {
        "patch": "Prior themes were resolved. Focus on the new findings only.",
        "backtrack": (
            "The themes above survived from the prior attempt. Re-examine"
            " your approach to these areas rather than adding more complexity."
        ),
        "escalate": (
            "These themes have now survived multiple attempts. Consider a"
            " fundamentally different approach to address them."
        ),
    }
    guidance = guidance_map.get(disposition, guidance_map["patch"])

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
