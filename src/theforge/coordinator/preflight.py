"""Coordinator preflight parsing, complexity adaptation, and model escalation."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

import yaml

from theforge.config import (
    AgentSpec,
    ForgeConfig,
    ModelInfo,
    ModelProfile,
    apply_model_info,
    model_info_view,
)
from theforge.config.profiles import _apply_provider_fallback
from theforge.review import ReviewFinding
from theforge.routing import DEV_COMPLEXITY_TIER, score_to_dev_tier

if TYPE_CHECKING:
    from .state import CoordinatorState

_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})

_log = logging.getLogger(__name__)

# Tier × complexity routing table for complexity-band fallbacks.
_PHASE_COMPLEXITY_TIER: dict[str, dict[str, str]] = {
    "dev": DEV_COMPLEXITY_TIER,
    "plan": {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"},
    "review": {"LOW": "mid", "MEDIUM": "mid", "HIGH": "strong"},
}

_TIER_TO_RANK: dict[str, int] = {"cheap": 1, "mid": 2, "strong": 3}

_COMPLEXITY_TO_LEVEL: dict[str, str] = {
    "small": "LOW",
    "medium": "MEDIUM",
    "large": "HIGH",
}

# Unambiguous concurrency vocabulary. A single hit is sufficient signal that a
# story is about concurrency control, because these terms rarely appear in
# non-concurrent prose.
_CONCURRENCY_STRONG_TERMS = (
    "asynchronous",
    "asyncio",
    "concurrency",
    "concurrent",
    "inter thread",
    "inter process",
    "multi thread",
    "multi threaded",
    "multiprocessing",
    "race condition",
    "deadlock",
    "mutex",
    "semaphore",
    "thread safe",
    "thread safety",
    "thread pool",
    "event loop",
)

# High-frequency terms that are common verbs/nouns in non-concurrent contexts
# ("thread a value through config", "kill the process", "async def helper").
# On their own these are NOT sufficient signal; they only count when a
# qualifying concurrency-context term co-occurs.
_CONCURRENCY_AMBIGUOUS_TERMS = (
    "thread",
    "threads",
    "process",
    "processes",
    "async",
)

# Context terms that qualify an ambiguous term as genuinely concurrency-related.
_CONCURRENCY_QUALIFIER_TERMS = (
    "lock",
    "locking",
    "parallel",
    "parallelism",
    "worker",
    "workers",
    "pool",
    "spawn",
    "spawns",
    "spawned",
    "await",
    "coroutine",
    "coroutines",
    "synchronize",
    "synchronized",
    "synchronization",
    "shared state",
    "shared memory",
    "race",
    "contention",
    "atomic",
    "scheduler",
    "executor",
    "background",
    "blocking",
    "non blocking",
    "gil",
)

_CANCELLATION_TERMS = (
    "cancel",
    "cancellation",
    "abort signal",
    "stop signal",
    "shutdown",
    "lifecycle",
)

_CROSS_BOUNDARY_TERMS = (
    "propagate",
    "propagation",
    "thread through",
    "threads through",
    "threading through",
    "across module",
    "across modules",
    "module boundaries",
    "boundary",
    "boundaries",
    "handoff",
    "handoffs",
)

_PHASE_TRANSITION_TERMS = (
    "multi phase",
    "multiple phases",
    "phase transition",
    "phase transitions",
    "phase boundary",
    "phase boundaries",
    "phase handoff",
    "phase handoffs",
    "across phases",
    "every phase",
    "state machine",
)

# NOTE: This heuristic must stay stack-neutral. The orchestrator sizes stories
# for *arbitrary* managed codebases, so classification signals may not embed
# TheForge's own architecture vocabulary ("runner", "engine", "coordinator",
# "phase"). Those words are common in unrelated build/CI/pipeline prose, so
# using them as triggers mis-sizes stories for every other project. Cross-module
# surgery is detected from stack-neutral signals (_CROSS_MODULE_TERMS +
# _COORDINATION_CHANGE_TERMS) instead. See issue #1139.
_COORDINATION_CHANGE_TERMS = (
    "modify",
    "modifies",
    "modification",
    "thread",
    "threads",
    "propagate",
    "propagation",
    "coordinate",
    "coordinated",
    "together",
    "all sites",
    "correctness depends",
)

_CROSS_MODULE_TERMS = (
    "cross module",
    "cross modules",
    "multi module",
    "multi modules",
    "module boundaries",
    "all sites",
    "correctness depends",
)

# Integer complexity scale bounds. Preflight emits a score in this range;
# out-of-range or missing scores fall back to the legacy three-level enum.
COMPLEXITY_SCORE_MIN = 1
COMPLEXITY_SCORE_MAX = 10


def score_to_band(score: int) -> str:
    """Map a 1-10 complexity score to the legacy small/medium/large enum.

    Bands: 1-3 → small, 4-7 → medium, 8-10 → large. This is the *compat shim*
    used by consumers that still read the string enum. Final bucketing policy
    belongs to each consumer — this function is just the default.
    """
    if score <= 3:
        return "small"
    if score <= 7:
        return "medium"
    return "large"


def band_to_score(band: str) -> int:
    """Map small/medium/large → a representative score (midpoint of each band).

    Used only as a fallback when a preflight output lacks ``complexity_score``
    so downstream code can treat the score as always-present.
    """
    norm = band.lower()
    if norm == "small":
        return 2
    if norm == "large":
        return 9
    return 5  # medium or unknown


def _normalize_story_text_for_match(text: str) -> str:
    """Normalize free-form story text for coarse phrase matching."""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return f" {normalized} " if normalized else " "


def _contains_any_phrase(normalized_text: str, phrases: tuple[str, ...]) -> bool:
    """Return True when any normalized phrase appears in normalized_text."""
    return any(
        f" {_normalize_story_text_for_match(phrase).strip()} " in normalized_text
        for phrase in phrases
    )


def _count_phrase_hits(normalized_text: str, phrases: tuple[str, ...]) -> int:
    """Count how many distinct normalized phrases appear in normalized_text."""
    return sum(
        1
        for phrase in phrases
        if f" {_normalize_story_text_for_match(phrase).strip()} " in normalized_text
    )


def _detect_large_preflight_story_categories(story_content: str) -> list[str]:
    """Return LARGE-trigger categories detected directly from story text.

    These categories are deterministic coordinator-side guardrails for work that
    tends to be under-sized by local edit count alone.
    """
    normalized = _normalize_story_text_for_match(story_content)
    matched: list[str] = []

    if _contains_any_phrase(normalized, _CONCURRENCY_STRONG_TERMS) or (
        _contains_any_phrase(normalized, _CONCURRENCY_AMBIGUOUS_TERMS)
        and _contains_any_phrase(normalized, _CONCURRENCY_QUALIFIER_TERMS)
    ):
        matched.append("concurrency control")

    if _contains_any_phrase(normalized, _CANCELLATION_TERMS) and _contains_any_phrase(
        normalized, _CROSS_BOUNDARY_TERMS
    ):
        matched.append("lifecycle/cancellation propagation across module boundaries")

    if _contains_any_phrase(normalized, _PHASE_TRANSITION_TERMS) and _contains_any_phrase(
        normalized, _COORDINATION_CHANGE_TERMS
    ):
        matched.append("multi-phase state-machine modifications")

    if _contains_any_phrase(normalized, _CROSS_MODULE_TERMS) and _contains_any_phrase(
        normalized, _COORDINATION_CHANGE_TERMS
    ):
        matched.append("cross-module coordinator surgery")

    return list(dict.fromkeys(matched))


def _build_pool_entries(
    model_keys: list[str],
    *,
    registry: dict[str, AgentSpec] | None = None,
) -> list[tuple[int, str, ModelInfo]]:
    """Build sorted (cost_rank, registry_key, ModelInfo) list from models.

    ``registry`` is the merged AgentSpec view (built-in + forge.yaml overlay)
    from ``ForgeConfig.model_registry``. When None, only built-ins are visible
    and custom-overlay model keys raise ValueError.
    """
    from theforge.config.models import (  # noqa: PLC0415
        _resolve_model_info,
        price_tiebreak_signal,
    )

    info_view = model_info_view(registry)
    entries: list[tuple[int, str, ModelInfo]] = []
    for key in model_keys:
        info = info_view.get(key)
        if info is None:
            info = _resolve_model_info(key, registry=registry)
        entries.append((info.cost_rank, key, info))
    # cost_rank asc, capability desc, then real per-MTok price so equal-tier /
    # equal-capability candidates break the tie by cost rather than list order (#1617).
    entries.sort(
        key=lambda x: (
            x[0],
            -x[2].capability,
            price_tiebreak_signal(x[2].input_cost_per_mtok, x[2].output_cost_per_mtok),
        )
    )
    return entries


def _pick_pool_entry_by_rank(
    entries: list[tuple[int, str, ModelInfo]],
    target_rank: int,
) -> ModelInfo:
    """Pick ModelInfo with target cost_rank; fall back to nearest available tier."""
    exact = [i for r, _, i in entries if r == target_rank]
    if exact:
        return exact[0]

    for rank in range(target_rank + 1, 4):
        higher = [i for r, _, i in entries if r == rank]
        if higher:
            return higher[0]

    for rank in range(target_rank - 1, 0, -1):
        lower = [i for r, _, i in entries if r == rank]
        if lower:
            return lower[0]

    return entries[0][2]


def _parse_preflight_verdict(output: str) -> tuple[str, str, bool]:
    """Extract verdict and reason from preflight agent output.

    Returns (verdict, reason, degraded). Parse failures and invalid verdicts
    return PROCEED with degraded=True — a confused classifier should not become
    process truth (same principle as the success=False path in preflight_flow).
    """
    # Extract YAML block from markdown fences
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return (
            "PROCEED",
            f"Failed to parse preflight YAML; falling back to PROCEED. Raw: {output[:200]}",
            True,
        )

    if not isinstance(parsed, dict):
        return "PROCEED", "Preflight output is not a dict; falling back to PROCEED.", True

    verdict = str(parsed.get("verdict", "PROCEED")).upper()
    reason = str(parsed.get("reason", "(no reason provided)"))

    if verdict not in _VALID_PREFLIGHT_VERDICTS:
        return (
            "PROCEED",
            f"Unknown preflight verdict {verdict!r}; falling back to PROCEED. {reason}",
            True,
        )

    return verdict, reason, False


_VALID_COMPLEXITIES = frozenset({"small", "medium", "large"})

_VALID_SUFFICIENCIES = frozenset({"implementation_ready", "needs_planning"})

_VALID_WORK_TYPES = frozenset({"feature", "refactor", "mechanical", "bug"})


def _parse_preflight_contract_change(output: str) -> bool:
    """Extract contract_change from preflight agent output. Defaults to False."""
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            raw = parsed.get("contract_change", False)
            if isinstance(raw, bool):
                return raw
            # Normalize string representations; reject non-boolean values safely
            if isinstance(raw, str) and raw.strip().lower() == "true":
                return True
            return False
    except yaml.YAMLError:
        pass

    return False


def _parse_preflight_work_type(output: str) -> str:
    """Extract work_type from preflight agent output. Defaults to 'feature' if absent."""
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            raw = str(parsed.get("work_type", "feature")).lower()
            if raw in _VALID_WORK_TYPES:
                return raw
    except yaml.YAMLError:
        pass

    return "feature"


def _parse_preflight_domains(output: str) -> list[str]:
    """Extract the ``domains`` list from preflight agent output.

    Returns a clean, ordered, de-duplicated list of canonical taxonomy tags
    (see :mod:`theforge.domains`). Tags outside the fixed taxonomy are dropped,
    mirroring how ``_parse_preflight_work_type`` rejects values outside its enum
    — unknown tags are never carried into routing. A missing field, a non-list
    value, or malformed YAML all yield ``[]`` (an explicit "no domains" fact,
    routing-safe as a current-run signal under ADR-0006 bucket A).
    """
    from theforge.domains import validate_domains  # noqa: PLC0415

    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            return validate_domains(parsed.get("domains"))
    except yaml.YAMLError:
        pass

    return []


def _parse_preflight_warnings(output: str) -> list[str]:
    """Extract warnings list from preflight agent output. Returns [] if absent."""
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            raw = parsed.get("warnings", [])
            if isinstance(raw, list):
                return [str(w) for w in raw if w]
    except yaml.YAMLError:
        pass

    return []


def _parse_preflight_likely_files(output: str) -> list[str] | None:
    """Extract likely_files list from preflight agent output.

    Returns None when the agent did not explicitly provide a valid list so that
    zero-footprint remains an explicit assertion rather than a parser default.
    """
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            if "likely_files" not in parsed:
                return None
            raw = parsed.get("likely_files")
            if isinstance(raw, list):
                return [str(path) for path in raw if path]
    except yaml.YAMLError:
        pass

    return None


def _parse_preflight_complexity(output: str) -> str:
    """Extract complexity from preflight agent output. Defaults to 'medium' if absent."""
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            raw = str(parsed.get("complexity", "medium")).lower()
            if raw in _VALID_COMPLEXITIES:
                return raw
    except yaml.YAMLError:
        pass

    return "medium"


def _parse_preflight_complexity_score(output: str, fallback_band: str | None = None) -> int | None:
    """Extract complexity_score from preflight agent output.

    Returns an int in [COMPLEXITY_SCORE_MIN, COMPLEXITY_SCORE_MAX]. Clamps
    out-of-range values to the bounds. When the field is absent or unparseable,
    derives a score from ``fallback_band`` (the legacy string enum) if given,
    else returns None so callers can detect the missing signal.
    """
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict) and "complexity_score" in parsed:
            raw = parsed.get("complexity_score")
            try:
                score = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                score = None
            if score is not None:
                if score < COMPLEXITY_SCORE_MIN:
                    return COMPLEXITY_SCORE_MIN
                if score > COMPLEXITY_SCORE_MAX:
                    return COMPLEXITY_SCORE_MAX
                return score
    except yaml.YAMLError:
        pass

    if fallback_band is not None:
        return band_to_score(fallback_band)
    return None


def _parse_preflight_sufficiency(output: str) -> str:
    """Extract sufficiency from preflight agent output.

    Returns 'implementation_ready' or 'needs_planning'.
    Defaults to 'needs_planning' on parse failure — fail-safe toward full pipeline.
    """
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            raw = str(parsed.get("sufficiency", "needs_planning")).lower()
            if raw in _VALID_SUFFICIENCIES:
                return raw
    except yaml.YAMLError:
        pass

    return "needs_planning"


def _parse_preflight_criteria_checked(output: str) -> list[dict]:
    """Extract criteria_checked list from preflight agent output.

    Each entry should have: criterion (str), files_checked (list[str]),
    runtime_path (str), satisfied (bool), evidence (str).

    Returns [] on parse failure, missing key, or non-list value so that
    callers can treat an absent map as insufficient evidence (conservative).
    """
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict):
            return []
        raw = parsed.get("criteria_checked")
        if not isinstance(raw, list):
            return []
        result = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            result.append(
                {
                    "criterion": str(entry.get("criterion", "")),
                    "files_checked": list(entry.get("files_checked") or []),
                    "runtime_path": str(entry.get("runtime_path") or ""),
                    "satisfied": bool(entry.get("satisfied", False)),
                    "evidence": str(entry.get("evidence") or ""),
                }
            )
        return result
    except yaml.YAMLError:
        return []


_VALID_SYMPTOM_VERIFICATION_STATUSES = frozenset(
    {"verified_resolved", "not_reproduced", "not_feasible", "not_attempted"}
)


def _parse_preflight_symptom_verification(output: str) -> dict:
    """Extract symptom_verification from preflight agent output.

    Returns a normalized dict with keys:
      - status: one of the valid status enum values, or "" when absent/invalid
      - evidence: stripped string, "" when absent
      - reproduces_now: bool | None — explicit signal that the symptom was
        exercised against the current baseline (False = not reproduced =
        symptom resolved); None when not asserted

    Returns {} when the field is absent so the caller can distinguish "agent
    did not address symptom" from "agent addressed and gave a status".
    """
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    raw = parsed.get("symptom_verification")
    if not isinstance(raw, dict):
        return {}

    status = str(raw.get("status", "")).strip().lower()
    if status not in _VALID_SYMPTOM_VERIFICATION_STATUSES:
        status = ""
    evidence = str(raw.get("evidence") or "").strip()
    reproduces_raw = raw.get("reproduces_now")
    if isinstance(reproduces_raw, bool):
        reproduces_now: bool | None = reproduces_raw
    else:
        reproduces_now = None
    return {
        "status": status,
        "evidence": evidence,
        "reproduces_now": reproduces_now,
    }


def _find_registry_info_for_profile(
    profile: ModelProfile,
    *,
    registry: dict[str, AgentSpec] | None = None,
) -> tuple[int, int]:
    """Return (cost_rank, capability) for a profile using the model registry.

    CLI profiles match by cli+model. API profiles match by provider+model against
    the registry key because registry entries are keyed by provider/model while
    storing the corresponding CLI transport.

    Falls back to (2, 5) for unknown models.
    """
    registry_key = _find_registry_key_for_profile(profile, registry=registry)
    if registry_key is None:
        return 2, 5
    info = model_info_view(registry)[registry_key]
    return info.cost_rank, info.capability


def _find_registry_key_for_profile(
    profile: ModelProfile,
    *,
    registry: dict[str, AgentSpec] | None = None,
) -> str | None:
    """Return the model registry key for a profile, or None if unknown.

    Matches by TransportSpec (single source of dispatch truth) plus model name.
    Falls back to cli/provider matching for profiles without an explicit
    transport.
    """
    profile_transport = profile.transport
    for key, info in model_info_view(registry).items():
        if info.model != profile.model:
            continue
        if profile_transport is not None and info.transport is not None:
            if (
                info.transport.kind == profile_transport.kind
                and info.transport.runner == profile_transport.runner
            ):
                return key
            continue
        if profile.cli is not None and info.cli == profile.cli:
            return key
        if (
            profile.cli is None
            and profile.provider is not None
            and info.provider == profile.provider
        ):
            return key
    return None


def _has_persistent_p1(
    current_findings: list[ReviewFinding],
    previous_findings: list[ReviewFinding],
) -> bool:
    """Return True if any P1 appears in both current and previous cycles.

    Matches when findings are text-similar (substring containment or
    >=60% token overlap) or when they recur at the same file+line location.
    """
    current_p1s = [f for f in current_findings if f.severity == "P1"]
    previous_p1s = [f for f in previous_findings if f.severity == "P1"]

    if not current_p1s or not previous_p1s:
        return False

    for curr in current_p1s:
        for prev in previous_p1s:
            if _p1_findings_match(curr, prev):
                return True

    return False


def _persistent_p1_descriptions(
    current_findings: list[ReviewFinding],
    previous_findings: list[ReviewFinding],
) -> list[str]:
    """Return description strings of current P1 findings that match previous P1 findings.

    Uses the same matching logic as _has_persistent_p1. Returns matched current
    descriptions, truncated to 200 chars.
    """
    current_p1s = [f for f in current_findings if f.severity == "P1"]
    previous_p1s = [f for f in previous_findings if f.severity == "P1"]

    if not current_p1s or not previous_p1s:
        return []

    matched: list[str] = []
    for curr in current_p1s:
        for prev in previous_p1s:
            if _p1_findings_match(curr, prev):
                matched.append(curr.description[:200])
                break

    return matched


def _p1_findings_match(current: ReviewFinding, previous: ReviewFinding) -> bool:
    """Return True when two P1 findings should be treated as the same persistent issue.

    A match requires file-level evidence plus either:
    - line proximity (within 10 lines of the same file), or
    - meaningful description overlap (substring or >=60% token overlap) in the same file.

    Description-only overlap without file evidence is rejected to avoid tagging distinct
    bugs in the same module as persistent when they happen to share vocabulary.
    When neither finding has a real file (both are "unknown"/null), description overlap
    alone is accepted as a fallback.
    """
    curr_has_file = bool(current.file and current.file != "unknown")
    prev_has_file = bool(previous.file and previous.file != "unknown")
    same_real_file = curr_has_file and prev_has_file and current.file == previous.file

    # Same file + line proximity (covers exact match and nearby edits)
    if same_real_file and current.line is not None and previous.line is not None:
        if abs(current.line - previous.line) <= 10:
            return True

    # Description-based matching
    desc_match = False
    if current.description and previous.description:
        if (
            current.description in previous.description
            or previous.description in current.description
        ):
            desc_match = True
        else:
            curr_tokens = set(re.findall(r"\w+", current.description.lower()))
            prev_tokens = set(re.findall(r"\w+", previous.description.lower()))
            if curr_tokens and prev_tokens:
                overlap = len(curr_tokens & prev_tokens) / max(len(curr_tokens), len(prev_tokens))
                if overlap >= 0.6:
                    desc_match = True

    if desc_match:
        # Require same real file, or accept when neither has a known file location
        if same_real_file or (not curr_has_file and not prev_has_file):
            return True

    return False


def _escalate_dev_model(
    current_model: str,
    available_models: list[str],
    *,
    registry: dict[str, AgentSpec] | None = None,
) -> str | None:
    """Return the next higher-capability dev-capable model, or None.

    Selects the lowest-capability model that is still higher than current
    and has ``dev_capable=True`` in the merged registry view.
    """
    info_view = model_info_view(registry)
    current_info = info_view.get(current_model)
    if current_info is None:
        return None

    candidates = [
        (key, info_view[key])
        for key in available_models
        if key in info_view
        and info_view[key].dev_capable
        and info_view[key].capability > current_info.capability
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1].capability)
    return candidates[0][0]


def _apply_complexity_adaptation(
    config: ForgeConfig,
    complexity: str,
    complexity_score: int | None = None,
) -> ForgeConfig:
    """Adjust model assignments based on preflight complexity using tier × complexity routing.

    Only applies when models is set. Per-role bypass flags guard each
    mutation so explicit forge.yaml overrides are preserved:
    - plan updates require config.plan_model_is_default
    - dev updates require config.dev_profile_is_default
    - review_pool updates require config.review_pool_is_default

    Tier × complexity routing (applied in-place via _dc_replace so load-time profile
    overrides like temperature/tools/budget are preserved on the updated profile):
      plan:   LOW → mid,    MEDIUM → strong,   HIGH → strong
      dev:    LOW → cheap,  MEDIUM → mid,      HIGH → strong
      review: LOW → single mid/strong reviewer (no synthesis)
              MEDIUM/HIGH → all mid/strong reviewers + synthesis
    """
    if config.models is None:
        return config

    norm = _COMPLEXITY_TO_LEVEL.get(complexity.lower())
    if norm is None:
        return config

    # Pass the registry through as-is: an explicitly empty {} must stay empty so
    # downstream resolution fails clearly rather than silently substituting the
    # built-in default (which `... or None` would trigger). See config.models.
    _registry = config.model_registry
    pool_entries = _build_pool_entries(config.models, registry=_registry)
    if not pool_entries:
        return config

    new_config = config

    # ── plan ───────────────────────────────────────────────────────
    if config.plan_model_is_default:
        target_plan_rank = _TIER_TO_RANK[_PHASE_COMPLEXITY_TIER["plan"][norm]]
        target_plan_info = _pick_pool_entry_by_rank(pool_entries, target_plan_rank)
        if target_plan_info.model != config.plan.model:
            new_plan = apply_model_info(config.plan, target_plan_info)
            new_config = _dc_replace(new_config, plan=new_plan)

    # ── dev ────────────────────────────────────────────────────────
    if config.dev_profile_is_default:
        dev_pool = [(r, k, i) for r, k, i in pool_entries if i.dev_capable] or pool_entries
        # Prefer the finer-grained numeric score for dev tier selection so
        # high-medium stories (score 7) route to strong instead of mid. Fall
        # back to the enum-based tier when no score is present.
        if complexity_score is not None:
            target_dev_rank = _TIER_TO_RANK[score_to_dev_tier(complexity_score)]
        else:
            target_dev_rank = _TIER_TO_RANK[_PHASE_COMPLEXITY_TIER["dev"][norm]]
        target_dev_info = _pick_pool_entry_by_rank(dev_pool, target_dev_rank)
        if target_dev_info.model != config.dev_profile.model:
            new_dev = apply_model_info(config.dev_profile, target_dev_info)
            new_dev = _apply_provider_fallback(new_dev, config.provider_fallbacks)
            new_config = _dc_replace(new_config, dev_profile=new_dev)

    # ── review_pool ────────────────────────────────────────────────
    if config.review_pool_is_default:
        # Self-review guard: if dev was rerouted into a model that's also in the
        # review pool, exclude it from review candidates. Match derive_roles()'s
        # load-time behavior where dev is excluded from review_pairs before tier
        # filtering. Only drop when alternatives exist — otherwise self-review is
        # the only option.
        new_dev_model = new_config.dev_profile.model
        non_dev_reviewers = [p for p in config.review_pool if p.model != new_dev_model]
        review_candidates = non_dev_reviewers if non_dev_reviewers else list(config.review_pool)

        mid_strong = [
            p
            for p in review_candidates
            if _find_registry_info_for_profile(p, registry=_registry)[0] >= 2
        ]

        if norm == "LOW":
            # Single mid/strong reviewer, no synthesis
            if not mid_strong:
                _log.warning(
                    "complexity_adaptation: LOW review: no mid/strong reviewers in pool, "
                    "falling back to cheapest reviewer"
                )
            candidate_pool = mid_strong or review_candidates
            single = min(
                candidate_pool,
                key=lambda p: (
                    _find_registry_info_for_profile(p, registry=_registry)[0],
                    -_find_registry_info_for_profile(p, registry=_registry)[1],
                ),
            )
            single = _apply_provider_fallback(single, config.provider_fallbacks)
            new_config = _dc_replace(new_config, review_pool=[single], synthesis_profile=None)
        else:
            # MEDIUM/HIGH → all mid/strong reviewers + synthesis
            review_broader = mid_strong if mid_strong else review_candidates
            review_broader = [
                _apply_provider_fallback(p, config.provider_fallbacks) for p in review_broader
            ]
            synthesis = config.synthesis_profile
            if synthesis is None:
                synth_candidates = review_broader or [new_config.dev_profile]
                strongest = max(
                    synth_candidates,
                    key=lambda p: _find_registry_info_for_profile(p, registry=_registry)[1],
                )
                synth_budget = max(config.dev_profile.budget_usd * 0.02, 1.0)
                synthesis = _dc_replace(strongest, name="synthesis", budget_usd=synth_budget)
                synthesis = _apply_provider_fallback(synthesis, config.provider_fallbacks)
            new_config = _dc_replace(
                new_config, review_pool=review_broader, synthesis_profile=synthesis
            )

    return new_config


def _emit_cap_downgrade_warning(
    log: Callable[[str], None],
    cap_audit: dict[str, object],
    downgraded_roles: set[str],
    *,
    story_slug: str | None,
) -> None:
    """Print a visible terminal warning when the per-story routing cost target forced a downgrade.

    The warning fires when ``_enforce_budget`` either dropped a reviewer or
    swapped a selected role for a cheaper one — i.e. adaptive's preferred
    selection was overridden by the cap.
    """
    if not cap_audit.get("downgraded"):
        return
    cap = cap_audit.get("target_usd")
    preferred = cap_audit.get("preferred") or {}
    if not isinstance(preferred, dict):
        return
    final_total = cap_audit.get("final_total_usd")
    initial_total = preferred.get("total_usd")
    pref_dev = (preferred.get("dev") or {}).get("model")
    pref_cr = [r.get("model") for r in preferred.get("code_reviewers", []) if isinstance(r, dict)]
    pref_pr = [r.get("model") for r in preferred.get("plan_reviewers", []) if isinstance(r, dict)]
    pref_planner = (preferred.get("planner") or {}).get("model")
    story_label = f"story {story_slug}" if story_slug else "story"
    log(
        f"  ⚠ {story_label}: per-story routing cost target ${cap:.2f} forces downgrade"
        if isinstance(cap, (int, float))
        else f"  ⚠ {story_label}: per-story routing cost target forces downgrade"
    )
    pref_total_str = f" @ ~${initial_total:.2f}" if isinstance(initial_total, (int, float)) else ""
    log(
        f"    adaptive selected: dev={pref_dev}, planner={pref_planner}, "
        f"plan_reviewers={pref_pr}, code_reviewers={pref_cr}{pref_total_str}"
    )
    final_total_str = f" @ ~${final_total:.2f}" if isinstance(final_total, (int, float)) else ""
    log(f"    after target:         downgraded roles={sorted(downgraded_roles)}{final_total_str}")


def _apply_preflight_config(
    config: ForgeConfig,
    state: "CoordinatorState",
    *,
    log: Callable[[str], None] | None = None,
    log_verbose: Callable[[str], None] | None = None,
    task_slug: str | None = None,
) -> ForgeConfig:
    """Apply complexity-driven config updates using values already stored on state."""
    complexity = state.preflight_complexity or "medium"
    complexity_score = state.preflight_complexity_score
    _log = log or (lambda _msg: None)
    _log_verbose = log_verbose or (lambda _msg: None)

    if config.models is not None:
        _config_before = config
        config = _apply_complexity_adaptation(config, complexity, complexity_score)
        _dev_changed = config.dev_profile.model != _config_before.dev_profile.model
        _plan_changed = config.plan.model != _config_before.plan.model
        _review_changed = [p.model for p in config.review_pool] != [
            p.model for p in _config_before.review_pool
        ]
        if _dev_changed or _plan_changed or _review_changed:
            state.complexity_routing_audit = {
                "complexity": complexity,
                "complexity_score": complexity_score,
                "derived_plan_model": config.plan.model,
                "derived_dev_model": config.dev_profile.model,
                "derived_review_pool": [p.model for p in config.review_pool],
                "source": "complexity_adaptive",
            }
            _log_verbose(
                f"[adaptive] complexity_routing: complexity={complexity} "
                f"dev={config.dev_profile.model} plan={config.plan.model} "
                f"review={[p.model for p in config.review_pool]}"
            )

    if not (config.assignment.enabled and config.agents):
        return config

    from theforge.assignment import (  # noqa: I001, PLC0415
        PHASE_TIER as _PHASE_TIER,
        _check_promotion as _chk_prom,
        _normalize_complexity as _norm_complexity,
        _pick_agent as _pick_agt,
        _promote_tier as _prom_tier,
        assign_models as _assign_models,
    )
    from theforge.config import (  # noqa: I001, PLC0415
        DEFAULT_DEV_PROFILE as _DEF_DEV,
        DEFAULT_PREFLIGHT_PROFILE as _DEF_PRE,
    )
    from theforge.coordinator.escalation_history import (  # noqa: PLC0415
        load_escalation_history_with_taint_stats as _load_esc_history_substrate,
    )

    # Escalation history plus the count of runs the centralized taint gate set
    # aside (ADR-0006 clause 4). The count is surfaced in the routing_decision
    # block (clause 7) so operators see how much history was discounted.
    _esc_history, _excluded_for_taint = _load_esc_history_substrate(config.project_root)

    from theforge.model_profiles import load_profiles as _load_profiles  # noqa: PLC0415

    _profiles_path = config.project_root / ".forge" / "model_profiles.yaml"
    _model_profiles = _load_profiles(_profiles_path)

    from theforge.provider_health import (  # noqa: PLC0415
        load_provider_health as _load_provider_health,
    )
    from theforge.provider_health import (
        unhealthy_models as _unhealthy_models,
    )

    _health_path = config.project_root / ".forge" / "provider_health.yaml"
    _unhealthy = _unhealthy_models(_load_provider_health(_health_path))

    from theforge.config import ModelProfile as _ModelProfile  # noqa: PLC0415

    _explicit: dict[str, object] = {}
    _explicit_roles: set[str] = set()
    _explicit_review_pool: list[_ModelProfile] = []
    _explicit_plan_review_pool: list[_ModelProfile] = []
    # Collect explicit overrides from both the legacy-agents path (models is None)
    # and the v0.8 models: path.  The is_default flags are authoritative regardless
    # of which YAML path set them, so the guard must not be limited to models is None.
    if config.models is None:
        if config.dev_profile is not _DEF_DEV:
            _explicit["dev"] = config.dev_profile
            _explicit_roles.add("dev")
        if config.preflight_profile is not _DEF_PRE:
            _explicit["preflight"] = config.preflight_profile
            _explicit_roles.add("preflight")
    # review_pool, plan, and plan_agent_review overrides apply on both paths.
    if config.review_pool and not config.review_pool_is_default:
        _explicit_roles.add("review_pool")
        _explicit_review_pool = list(config.review_pool)
        # Lock code_review against budget downgrade and audit it as overridden.
        _explicit["code_review"] = _explicit_review_pool[0]
    if not config.plan_model_is_default:
        _explicit_roles.add("planner")
        _explicit_planner = _ModelProfile(
            name="plan",
            cli=config.plan.cli,
            model=config.plan.model,
            provider=config.plan.provider,
            budget_usd=config.plan.budget_usd,
            timeout_seconds=config.plan.timeout,
            allowed_tools=config.preflight_profile.allowed_tools,
            api_fallback=config.plan.api_fallback,
            phase="plan",
        )
        _explicit["planner"] = _explicit_planner
    if config.plan_agent_review.enabled and config.plan_agent_review.profiles:
        _explicit_roles.add("plan_agent_review")
        _explicit_plan_review_pool = list(config.plan_agent_review.profiles)
        _explicit["plan_review"] = _explicit_plan_review_pool[0]

    # Challenger-sampling exploration budget (#325, ADR-0006 clause 8 "bounded"):
    # at most per_sprint_cap exploration runs across the whole sprint. The
    # advisory remaining count avoids deciding a challenger when the cap is
    # already spent; the AUTHORITATIVE, race-free consume is reserve_slot() below.
    from theforge.coordinator import exploration_budget as _explore_budget  # noqa: PLC0415

    _explore_cap = int(getattr(config.assignment.exploration, "per_sprint_cap", 0) or 0)

    def _assign(explore_budget: int | None):
        return _assign_models(
            config.agents,
            config.assignment,
            complexity,
            complexity_score=complexity_score,
            escalation_history=_esc_history,
            explicit_profiles=_explicit if _explicit else None,
            sprint_promotions=state.sprint_promotions,
            secrets=config.secrets,
            model_profiles=_model_profiles,
            unhealthy_models=_unhealthy if _unhealthy else None,
            domains=list(state.preflight_domains or []),
            excluded_for_taint=_excluded_for_taint,
            sprint_exploration_budget=explore_budget,
        )

    _explore_remaining = _explore_budget.remaining_budget(
        config.project_root, state.sprint_name, _explore_cap
    )
    _decision = _assign(_explore_remaining)

    # If a challenger fired, ATOMICALLY claim a sprint slot before honoring it.
    # Two parallel workers can both see one free slot in the advisory read; only
    # one wins the O_EXCL claim in reserve_slot(), so the per-sprint cap holds
    # exactly. The loser re-derives its decision in winner mode.
    _explore_block = getattr(_decision, "routing_decision", None)
    if isinstance(_explore_block, dict):
        _dev_explore = (_explore_block.get("dev") or {}).get("exploration") or {}
        if _dev_explore.get("mode") == "challenger":
            _claimed = _explore_budget.reserve_slot(
                config.project_root,
                state.sprint_name,
                _explore_cap,
                {
                    "story": task_slug,
                    "routing_key": _dev_explore.get("routing_key"),
                    "challenger": _dev_explore.get("selected"),
                    "winner": _dev_explore.get("winner"),
                    "reason": _dev_explore.get("reason"),
                },
            )
            if not _claimed:
                # Lost the race / cap already reached — re-route on-policy so the
                # sprint-wide bound is never exceeded.
                _decision = _assign(0)
                _explore_block = getattr(_decision, "routing_decision", None)
                _dev_explore = ((_explore_block or {}).get("dev") or {}).get("exploration") or {}
            elif _dev_explore.get("selected"):
                # A steady-state challenger actually REPLACED the winner for the
                # dev slot. Stash the recovery target: re-derive the winner-mode
                # decision so its dev profile is available if the challenger
                # attempt fails (clause-8 recoverable). Use budget 0 (cap reached
                # → winner mode) NOT None (disabled → deterministic), so recovery
                # routes through the SAME audit-derived empirical winner the block
                # records — not the stale static-tier incumbent.
                _winner_decision = _assign(0)
                state.exploration_challenger = {
                    "routing_key": _dev_explore.get("routing_key"),
                    "challenger": _dev_explore.get("selected"),
                    "winner": _dev_explore.get("winner"),
                    "pool": list(_dev_explore.get("pool") or []),
                }
                state.exploration_winner_dev_profile = _winner_decision.dev

    # Splice the full explicit pools back into the decision so audit and
    # downstream consumers see the models that actually run, then recompute
    # the budget audit total against the runtime configuration so explicit
    # planner/reviewer overrides cannot silently overrun the cap.
    if _explicit_review_pool or _explicit_plan_review_pool:
        _decision = _dc_replace(
            _decision,
            code_reviewers=(
                _explicit_review_pool if _explicit_review_pool else _decision.code_reviewers
            ),
            plan_reviewers=(
                _explicit_plan_review_pool
                if _explicit_plan_review_pool
                else _decision.plan_reviewers
            ),
        )

    if _explicit:
        _runtime_total = (
            _decision.preflight.budget_usd
            + _decision.planner.budget_usd
            + sum(p.budget_usd for p in _decision.plan_reviewers)
            + _decision.dev.budget_usd
            + sum(p.budget_usd for p in _decision.code_reviewers)
        )
        _cap = config.assignment.max_cost_per_story_usd
        _new_audit = dict(_decision.budget_audit)
        _new_audit["final_total_usd"] = round(_runtime_total, 2)
        if _cap is not None:
            _within = _runtime_total <= _cap
            _new_audit["within_target"] = _within
            if not _within:
                _new_audit["override_forced_overrun"] = True
                _new_audit["locked_roles"] = sorted(set(_explicit.keys()))
        _decision = _dc_replace(_decision, budget_audit=_new_audit)

    _replace_kwargs: dict[str, object] = {
        "dev_profile": _decision.dev,
        "preflight_profile": _decision.preflight,
    }
    if _decision.code_reviewers:
        if "review_pool" not in _explicit_roles:
            _replace_kwargs["review_pool"] = _decision.code_reviewers
        else:
            _log("  [adaptive] review_pool: explicit override preserved")
    if _decision.plan_reviewers:
        if "plan_agent_review" not in _explicit_roles:
            _replace_kwargs["plan_agent_review"] = _dc_replace(
                config.plan_agent_review,
                enabled=True,
                pool=_decision.plan_reviewers,
            )
            _log_verbose(
                f"[adaptive] plan_agent_review: enabled=True, "
                f"pool={[p.model for p in _decision.plan_reviewers]}"
            )
        else:
            _log("  [adaptive] plan_agent_review: explicit override preserved")
    config = _dc_replace(config, **_replace_kwargs)

    state._adaptive_decision = _decision
    state._explicit_roles = _explicit_roles
    # Carry the structured routing explainability block (#1391) into run state
    # so it persists as a top-level routing_decision key in the native per-run
    # audit record. Origin is labeled "preflight" in each final rationale so a
    # future post-assignment checkpoint (#1387) writing the same block stays
    # distinguishable. Kept separate from complexity_routing_audit (which retains
    # its existing outcome-only shape) per ADR-0006 clause 7.
    _routing_block = getattr(_decision, "routing_decision", None)
    if _routing_block:
        # assign_models sees only the first override profile per reviewer role, so
        # when a multi-profile explicit review_pool / plan_agent_review pool was
        # spliced into _decision above, the block's final.models/candidate_pool
        # under-report the reviewers that will actually run. Reconcile the affected
        # reviewer roles from the post-splice pools so the persisted block stays
        # consistent with runtime (#1391 iter1).
        if _explicit_review_pool or _explicit_plan_review_pool:
            from theforge.assignment import (  # noqa: PLC0415
                reconcile_explicit_reviewer_pools as _reconcile_reviewers,
            )

            _reconcile_reviewers(
                _routing_block,
                config.agents,
                plan_reviewers=(_decision.plan_reviewers if _explicit_plan_review_pool else None),
                code_reviewers=(_decision.code_reviewers if _explicit_review_pool else None),
                secrets=config.secrets,
            )
        state.routing_decision = _routing_block
    _existing_routing_audit = dict(state.complexity_routing_audit or {})
    _adaptive_enabled = config.assignment.adaptive_enabled
    _cap_audit = dict(_decision.budget_audit)
    _cap_downgraded_roles = {
        str(step.get("role"))
        for step in _cap_audit.get("steps", [])
        if isinstance(step, dict) and step.get("role")
    }
    _emit_cap_downgrade_warning(_log, _cap_audit, _cap_downgraded_roles, story_slug=task_slug)

    # Map audit roles to whichever explicit-source hint indicates an override.
    # _explicit (passed to assign_models) keys: dev/preflight/planner/code_review/plan_review
    # _explicit_roles (config-derived) keys: dev/preflight/planner/review_pool/plan_agent_review
    _override_keys = set(_explicit.keys()) if isinstance(_explicit, dict) else set()
    _audit_role_to_explicit_role = {
        "preflight": "preflight",
        "planner": "planner",
        "plan_review": "plan_agent_review",
        "dev": "dev",
        "code_review": "review_pool",
    }

    def _role_source(role: str) -> str:
        if role in _override_keys or _audit_role_to_explicit_role.get(role) in _explicit_roles:
            return "explicit_override"
        if not _adaptive_enabled:
            return "static"
        if role in _cap_downgraded_roles:
            return "cap_downgrade"
        return "adaptive"

    _per_role_sources = {
        "preflight": _role_source("preflight"),
        "planner": _role_source("planner"),
        "plan_review": _role_source("plan_review"),
        "dev": _role_source("dev"),
        "code_review": _role_source("code_review"),
    }

    def _assignment_entry(profile):  # type: ignore[no-untyped-def]
        from theforge.model_profiles import canonical_id_from_identity  # noqa: PLC0415

        canonical = canonical_id_from_identity(
            actual_model=getattr(profile, "model", None),
            provider=getattr(profile, "provider", None),
            cli=getattr(profile, "cli", None),
        )
        return {
            "model": profile.model,
            "source": getattr(profile, "registry_source", "builtin"),
            "canonical_id": canonical or "",
        }

    state.complexity_routing_audit = {
        "complexity": complexity,
        "complexity_score": complexity_score,
        "adaptive_enabled": _adaptive_enabled,
        "source": "adaptive_assignment" if _adaptive_enabled else "static_assignment",
        "explicit_overrides": sorted(_explicit_roles),
        "role_sources": _per_role_sources,
        "assignments": {
            "preflight": _assignment_entry(_decision.preflight),
            "planner": _assignment_entry(_decision.planner),
            "plan_reviewers": [_assignment_entry(p) for p in _decision.plan_reviewers],
            "dev": _assignment_entry(_decision.dev),
            "code_reviewers": [_assignment_entry(p) for p in _decision.code_reviewers],
        },
        "rationale": dict(_decision.rationale),
        "per_story_routing_cost_target": _cap_audit,
        **({"config_model_routing": _existing_routing_audit} if _existing_routing_audit else {}),
    }

    _dev_base_tier = _PHASE_TIER["dev"][_norm_complexity(complexity)]
    _dev_agent = _pick_agt(
        config.agents,
        _dev_base_tier,
        config.secrets,
        model_profiles=_model_profiles if config.assignment.adaptive_enabled else None,
        role="dev",
        complexity=_norm_complexity(complexity),
        recency=config.assignment.recency if config.assignment.adaptive_enabled else None,
    )
    _dev_name = _dev_agent.name if _dev_agent else ""
    if (
        config.assignment.adaptive_enabled
        and _dev_name
        and "dev" not in _explicit
        and complexity not in state.sprint_promotions
    ):
        # Profile-backed dev pre-promotion (#158): sticky-cache the tier bump so a
        # band that pre-promotes early in a sprint stays promoted for the sprint
        # (within-sprint stability). Recovery is the cross-run path — a fresh
        # sprint starts with an empty cache and re-evaluates the recency signal.
        _prom = _chk_prom(
            _norm_complexity(complexity),
            _dev_agent,
            _model_profiles,
            threshold=config.assignment.dev_promotion_threshold,
            min_runs=config.assignment.dev_promotion_min_runs,
            recency=config.assignment.recency,
            sprint_promotions=state.sprint_promotions,
        )
        if _prom.fired:
            _promoted_tier = _prom_tier(_dev_base_tier)
            state.sprint_promotions[_norm_complexity(complexity)] = _promoted_tier
            _log_verbose(
                f"[adaptive] {_norm_complexity(complexity)} dev pre-promoted "
                f"{_dev_name} -> tier {_promoted_tier} (sticky for sprint)"
            )

    _log_verbose(f"[adaptive] Complexity: {_norm_complexity(complexity)} (from preflight)")
    for _phase, _rsn in _decision.rationale.items():
        _log_verbose(f"[adaptive] {_phase}: {_rsn}")

    return config
