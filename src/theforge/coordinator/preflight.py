"""Coordinator preflight parsing, complexity adaptation, and model escalation."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

import yaml

from theforge.config import (
    DEFAULT_INVESTIGATION_TOOLS,
    AgentSpec,
    ForgeConfig,
    ModelInfo,
    ModelProfile,
    apply_model_info,
    model_info_view,
)
from theforge.config.bridge import model_ref_to_profile
from theforge.config.model_identity import PHASE_PLAN
from theforge.config.profiles import _apply_transport_fallback
from theforge.policy_provenance import (
    BLOCKING_BASES,
    PolicyAssertionCitation,
    parse_citations,
)
from theforge.review import ReviewFinding
from theforge.routing import DEV_COMPLEXITY_TIER, score_to_dev_tier

if TYPE_CHECKING:
    from pathlib import Path

    from .state import CoordinatorState

_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})

#: Source label for a complexity that no preflight agent founded (#2346). A
#: degraded preflight still yields a conservative score so routing, timeouts,
#: and allocation have a number to work from — but that number rests on the
#: coordinator's fallback, not on anything the classifier observed. Every
#: surface that reports complexity says which of the two it is.
COMPLEXITY_SOURCE_PREFLIGHT = "preflight"
COMPLEXITY_SOURCE_DEGRADED = "preflight_degraded_conservative"


def complexity_source(state: "CoordinatorState") -> str:
    """Return the provenance label for this story's complexity figure."""
    return (
        COMPLEXITY_SOURCE_DEGRADED
        if getattr(state, "preflight_degraded", False)
        else COMPLEXITY_SOURCE_PREFLIGHT
    )


def complexity_is_founded(state: "CoordinatorState") -> bool:
    """Whether this story's complexity figure rests on something preflight saw.

    The two signals are the ones :mod:`.resume_persistence` already weighs when
    it decides which attempt a resume proceeds from, in the same order:

    1. *degraded* — a marked-degraded attempt derived its score from a failure
       rather than an observation, and derives a deliberately **higher** one to
       buy accommodation while observing nothing.
    2. *examined* — ``criteria_checked``. An attempt that checked nothing cannot
       found a claim about the story, whatever band it named.

    Routing, timeouts, and allocation are right to consume an unfounded score:
    they need *a* number and a conservative one is the safe guess. A question
    put to an operator is not: "is this story too broad?" answered from a figure
    that means "we could not tell" asks them to rule on nothing, and any gate
    that fails closed on silence would then return the story on no evidence at
    all. So this predicate guards the gate, not the routing.
    """
    if getattr(state, "preflight_degraded", False):
        return False
    return bool(getattr(state, "preflight_criteria_checked", None))


def stamp_complexity_provenance(state: "CoordinatorState") -> None:
    """Record on the routing audit what founded the complexity it routed on.

    Called at every exit from :func:`_apply_preflight_config`, because the audit
    dict is assembled by several different branches (config-model adaptation,
    the assignment router, the allocation writer) and a provenance label that
    only some of them carry is one an operator cannot rely on.
    """
    audit = dict(state.complexity_routing_audit or {})
    audit["complexity_source"] = complexity_source(state)
    audit["preflight_degraded"] = degraded_preflight_fields(state)
    state.complexity_routing_audit = audit


def degraded_preflight_fields(state: "CoordinatorState") -> dict[str, object]:
    """Return the degraded-preflight block shared by every reporting surface."""
    return {
        "degraded": bool(getattr(state, "preflight_degraded", False)),
        "degraded_reason": getattr(state, "preflight_degraded_reason", None),
        "failure_action": getattr(state, "preflight_failure_action", None),
        "risk_signals": list(getattr(state, "preflight_risk_signals", None) or []),
        "complexity_source": complexity_source(state),
    }


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
    from theforge.config.models import _resolve_model_info  # noqa: PLC0415
    from theforge.config.pricing import price_tiebreak_signal_for  # noqa: PLC0415

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
            price_tiebreak_signal_for(x[2]),
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
    parsed, error = _load_preflight_yaml(output)
    if error is not None:
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


def _extract_preflight_yaml_text(output: str) -> str:
    """Return the outer fenced YAML body when present, else the raw output.

    Preflight responses sometimes wrap the YAML block in longer fences so the
    body can include ordinary triple-backtick snippets after the structured
    classification. The parser contract here is conservative: only an
    unindented closing fence with at least the opening fence length ends the
    YAML body, while indented fences or shorter inner fences remain part of the
    YAML content.
    """
    match = _select_preflight_fence_opening(output)
    if match is None:
        return output

    start = match.end()
    opening_len = len(match.group("fence"))
    close_re = re.compile(rf"^(?P<fence>`{{{opening_len},}})[ \t]*$", re.MULTILINE)
    close_match = close_re.search(output[start:])
    if close_match is None:
        return output[start:]

    end = start + close_match.start()
    return output[start:end]


def _select_preflight_fence_opening(output: str) -> re.Match[str] | None:
    """Prefer a YAML-tagged opening fence; otherwise use the first bare fence."""
    fence_re = re.compile(r"^(?P<fence>`{3,})(?P<label>[^\n`]*)$", re.MULTILINE)
    bare_match: re.Match[str] | None = None
    for match in fence_re.finditer(output):
        label = (match.group("label") or "").strip().lower()
        if not label:
            if bare_match is None:
                bare_match = match
            continue
        if label == "yaml":
            return match
    return bare_match


def _load_preflight_yaml(output: str) -> tuple[object | None, yaml.YAMLError | None]:
    """Load the selected preflight YAML block, preserving parse-failure detail."""
    yaml_text = _extract_preflight_yaml_text(output)
    try:
        return yaml.safe_load(yaml_text), None
    except yaml.YAMLError as exc:
        return None, exc


def _parse_preflight_mapping(output: str) -> dict[str, object] | None:
    """Return the parsed preflight mapping when the selected block is valid."""
    parsed, error = _load_preflight_yaml(output)
    if error is not None or not isinstance(parsed, dict):
        return None
    return parsed


def _parse_preflight_contract_change(output: str) -> bool:
    """Extract contract_change from preflight agent output. Defaults to False."""
    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        raw = parsed.get("contract_change", False)
        if isinstance(raw, bool):
            return raw
        # Normalize string representations; reject non-boolean values safely
        if isinstance(raw, str) and raw.strip().lower() == "true":
            return True

    return False


def _parse_preflight_work_type(output: str) -> str:
    """Extract work_type from preflight agent output. Defaults to 'feature' if absent."""
    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        raw = str(parsed.get("work_type", "feature")).lower()
        if raw in _VALID_WORK_TYPES:
            return raw

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

    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        return validate_domains(parsed.get("domains"))

    return []


def _parse_preflight_warnings(output: str) -> list[str]:
    """Extract warnings list from preflight agent output. Returns [] if absent."""
    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        raw = parsed.get("warnings", [])
        if isinstance(raw, list):
            return [str(w) for w in raw if w]

    return []


def _parse_preflight_likely_files(output: str) -> list[str] | None:
    """Extract likely_files list from preflight agent output.

    Returns None when the agent did not explicitly provide a valid list so that
    zero-footprint remains an explicit assertion rather than a parser default.
    """
    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        if "likely_files" not in parsed:
            return None
        raw = parsed.get("likely_files")
        if isinstance(raw, list):
            return [str(path) for path in raw if path]

    return None


def _parse_preflight_complexity(output: str) -> str:
    """Extract complexity from preflight agent output. Defaults to 'medium' if absent."""
    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        raw = str(parsed.get("complexity", "medium")).lower()
        if raw in _VALID_COMPLEXITIES:
            return raw

    return "medium"


def _parse_preflight_complexity_score(output: str, fallback_band: str | None = None) -> int | None:
    """Extract complexity_score from preflight agent output.

    Returns an int in [COMPLEXITY_SCORE_MIN, COMPLEXITY_SCORE_MAX]. Clamps
    out-of-range values to the bounds. When the field is absent or unparseable,
    derives a score from ``fallback_band`` (the legacy string enum) if given,
    else returns None so callers can detect the missing signal.
    """
    parsed = _parse_preflight_mapping(output)
    if parsed is not None and "complexity_score" in parsed:
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

    if fallback_band is not None:
        return band_to_score(fallback_band)
    return None


def _parse_preflight_scope_exceeded(output: str) -> bool | None:
    """Extract ``scope_exceeded`` from preflight agent output.

    Returns the emitted boolean, or None when the field is absent or is not a
    real boolean. None means "the classifier said nothing" — deliberately
    distinct from ``False``, so the caller can tell a silent model from one
    that asserted the story is within single-story scope. Parsing is strict:
    a string, number, or list is not a claim about scope and is treated as
    absent rather than coerced into truthiness.
    """
    parsed = _parse_preflight_mapping(output)
    if parsed is None:
        return None
    raw = parsed.get("scope_exceeded")
    if isinstance(raw, bool):
        return raw
    return None


def _parse_preflight_sufficiency(output: str) -> str:
    """Extract sufficiency from preflight agent output.

    Returns 'implementation_ready' or 'needs_planning'.
    Defaults to 'needs_planning' on parse failure — fail-safe toward full pipeline.
    """
    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        raw = str(parsed.get("sufficiency", "needs_planning")).lower()
        if raw in _VALID_SUFFICIENCIES:
            return raw

    return "needs_planning"


def _parse_preflight_criteria_checked(output: str) -> list[dict]:
    """Extract criteria_checked list from preflight agent output.

    Each entry should have: criterion (str), files_checked (list[str]),
    runtime_path (str), satisfied (bool), evidence (str).

    Returns [] on parse failure, missing key, or non-list value so that
    callers can treat an absent map as insufficient evidence (conservative).
    """
    parsed = _parse_preflight_mapping(output)
    if parsed is None:
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


def _parse_preflight_blocking_basis(output: str) -> str:
    """Extract ``blocking_basis`` from preflight output.

    Returns ``"none"`` when absent or outside the fixed enum. The value only
    matters for BLOCKED verdicts, where it names *which kind* of blocker fired so
    the coordinator can adjudicate policy-assertion blockers (#2137) without
    touching missing-credential or direct-contradiction refusals.
    """
    parsed = _parse_preflight_mapping(output)
    if parsed is not None:
        raw = str(parsed.get("blocking_basis", "")).strip().lower()
        if raw in BLOCKING_BASES:
            return raw
    return "none"


def _parse_preflight_policy_assertions(output: str) -> list[PolicyAssertionCitation]:
    """Extract ``policy_assertions_cited`` from preflight output.

    Returns ``[]`` when the field is absent or unreadable. An absent citation list
    is a fact, not a failure: it means the refusal named no standing policy, and
    the caller weighs that against the reason prose itself.
    """
    parsed = _parse_preflight_mapping(output)
    if parsed is None:
        return []
    return parse_citations(parsed.get("policy_assertions_cited"))


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
    parsed = _parse_preflight_mapping(output)
    if parsed is None:
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
            new_dev = _apply_transport_fallback(new_dev, config.transport_fallbacks)
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
            single = _apply_transport_fallback(single, config.transport_fallbacks)
            new_config = _dc_replace(new_config, review_pool=[single], synthesis_profile=None)
        else:
            # MEDIUM/HIGH → all mid/strong reviewers + synthesis
            review_broader = mid_strong if mid_strong else review_candidates
            review_broader = [
                _apply_transport_fallback(p, config.transport_fallbacks) for p in review_broader
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
                synthesis = _apply_transport_fallback(synthesis, config.transport_fallbacks)
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


def _plan_review_profiles(config: ForgeConfig) -> list[ModelProfile]:
    """Return the plan-review profiles that will actually run, else empty.

    ``PlanAgentReviewConfig.profiles`` synthesizes a legacy single profile even
    when the role is disabled, so a disabled role would otherwise claim a share
    of the allocation no phase ever spends.
    """
    if not config.plan_agent_review.enabled:
        return []
    return list(config.plan_agent_review.profiles or [])


def _configured_story_budget(config: ForgeConfig) -> float:
    """Return the configured per-story budget the allocation falls back to.

    The v0.8 ``models:`` path records the operator's per-story number directly
    in ``models_budget_usd``. The legacy explicit-profiles path has no single
    number, so the equivalent is the sum of the configured role budgets — the
    total the run was authorized to spend before any derivation.
    """
    if config.models_budget_usd is not None:
        return float(config.models_budget_usd)
    total = (
        float(config.dev_profile.budget_usd)
        + float(config.preflight_profile.budget_usd)
        + float(config.plan.budget_usd)
        + sum(float(p.budget_usd) for p in config.review_pool)
        + sum(float(p.budget_usd) for p in _plan_review_profiles(config))
    )
    if config.synthesis_profile is not None:
        total += float(config.synthesis_profile.budget_usd)
    return total


def _role_budget_map(config: ForgeConfig) -> dict[str, float]:
    """Return the runtime per-role budget map keyed by stable role labels."""
    budgets: dict[str, float] = {
        "preflight": float(config.preflight_profile.budget_usd),
        "plan": float(config.plan.budget_usd),
        "dev": float(config.dev_profile.budget_usd),
    }
    for index, profile in enumerate(config.review_pool):
        budgets[f"review_pool[{index}]"] = float(profile.budget_usd)
    for index, profile in enumerate(_plan_review_profiles(config)):
        budgets[f"plan_agent_review[{index}]"] = float(profile.budget_usd)
    if config.synthesis_profile is not None:
        budgets["synthesis"] = float(config.synthesis_profile.budget_usd)
    return budgets


def _locked_role_labels(config: ForgeConfig, explicit_roles: set) -> set[str]:
    """Return the role labels whose budgets an explicit override pins in place."""
    locked: set[str] = set()
    if "dev" in explicit_roles:
        locked.add("dev")
    if "preflight" in explicit_roles:
        locked.add("preflight")
    if "planner" in explicit_roles:
        locked.add("plan")
    if "review_pool" in explicit_roles:
        locked.update(f"review_pool[{i}]" for i in range(len(config.review_pool)))
    if "plan_agent_review" in explicit_roles:
        locked.update(f"plan_agent_review[{i}]" for i in range(len(_plan_review_profiles(config))))
    return locked


def _carried_story_allocation(state: "CoordinatorState") -> dict | None:
    """Return the allocation already on state when it still applies, else None.

    Reuse is conditional on the band being the same one it was derived for. A
    resumed attempt whose preflight lands on a different complexity score is a
    different story-shape than the record describes, so its allocation is
    re-derived rather than inherited — carrying it forward would judge this
    attempt's spend against another band's distribution.
    """
    carried = state.story_allocation
    if not isinstance(carried, dict) or not carried:
        return None
    try:
        float(carried["allocation_usd"])
    except (KeyError, TypeError, ValueError):
        return None
    if carried.get("complexity_score") != state.preflight_complexity_score:
        return None
    return carried


def _apply_story_allocation(
    config: ForgeConfig,
    state: "CoordinatorState",
    *,
    log_verbose: Callable[[str], None],
) -> ForgeConfig:
    """Derive this story's allocation from its complexity band and install it.

    Runs after the complexity score is known and after the runtime profiles are
    resolved, so the rescaled shares govern exactly the models that will run.
    The derivation is pure arithmetic over recorded costs (see
    :mod:`theforge.coordinator.story_budget`); no model is consulted.

    An allocation already on state — restored from the resume record, or set by
    an earlier call this run (routing recovery re-enters here) — is REUSED
    rather than re-derived, provided it was derived for the same complexity
    score. The substrate grows between attempts, so re-deriving would silently
    change the number a resumed story is judged against mid-story: the spend
    already on the clock was incurred under the first attempt's allocation. The
    per-role shares are always recomputed, because ``config`` is resolved fresh
    on every entry and the carried record's shares may not match its roles.
    """
    from .story_budget import derive_story_allocation, scale_role_budgets  # noqa: PLC0415

    carried = _carried_story_allocation(state)
    if carried is not None:
        record = dict(carried)
        allocation_usd = float(record["allocation_usd"])
    else:
        # Read the configured fallback only on the deriving path: on a re-entry
        # the profiles have already been rescaled, so their sum is the previous
        # allocation rather than the operator's configured per-story budget.
        allocation = derive_story_allocation(
            config.project_root,
            complexity_score=state.preflight_complexity_score,
            configured_usd=_configured_story_budget(config),
        )
        record = allocation.as_dict()
        allocation_usd = allocation.allocation_usd

    current = _role_budget_map(config)
    locked = _locked_role_labels(config, set(state._explicit_roles or set()))
    scaled = scale_role_budgets(current, allocation_usd, locked=locked)

    replace_kwargs: dict[str, object] = {}
    if scaled.get("preflight") != current.get("preflight"):
        replace_kwargs["preflight_profile"] = _dc_replace(
            config.preflight_profile, budget_usd=scaled["preflight"]
        )
    if scaled.get("plan") != current.get("plan"):
        replace_kwargs["plan"] = _dc_replace(
            config.plan, ref=_dc_replace(config.plan.ref, budget_usd=scaled["plan"])
        )
    if scaled.get("dev") != current.get("dev"):
        replace_kwargs["dev_profile"] = _dc_replace(config.dev_profile, budget_usd=scaled["dev"])
    new_pool = [
        _dc_replace(profile, budget_usd=scaled[f"review_pool[{i}]"])
        for i, profile in enumerate(config.review_pool)
    ]
    if new_pool:
        replace_kwargs["review_pool"] = new_pool
    _plan_reviewers = _plan_review_profiles(config)
    if _plan_reviewers:
        _scaled_plan_reviewers = [
            _dc_replace(profile, budget_usd=scaled[f"plan_agent_review[{i}]"])
            for i, profile in enumerate(_plan_reviewers)
        ]
        # ``profiles`` is a read-only view over ``pool`` (or the single composed
        # ``ref``). Write back through whichever one the config actually uses.
        if config.plan_agent_review.pool:
            replace_kwargs["plan_agent_review"] = _dc_replace(
                config.plan_agent_review, pool=_scaled_plan_reviewers
            )
        elif config.plan_agent_review.ref is not None:
            replace_kwargs["plan_agent_review"] = _dc_replace(
                config.plan_agent_review,
                ref=_dc_replace(
                    config.plan_agent_review.ref,
                    budget_usd=_scaled_plan_reviewers[0].budget_usd,
                ),
            )
    if config.synthesis_profile is not None:
        replace_kwargs["synthesis_profile"] = _dc_replace(
            config.synthesis_profile, budget_usd=scaled["synthesis"]
        )
    if replace_kwargs:
        config = _dc_replace(config, **replace_kwargs)

    record["scaled_profiles"] = {name: round(value, 4) for name, value in scaled.items()}
    if carried is not None:
        # Convention 6: a carried allocation must be traceable as one. Without
        # this an operator reading the audit cannot tell an allocation this
        # attempt derived from one it inherited.
        record["carried"] = True
    state.story_allocation = record
    routing_audit = dict(state.complexity_routing_audit or {})
    routing_audit["story_allocation"] = state.story_allocation
    state.complexity_routing_audit = routing_audit
    log_verbose(
        f"[adaptive] story_allocation: ${allocation_usd:.2f} "
        f"basis={record.get('basis')} "
        f"({'carried from prior attempt' if carried is not None else 'derived this attempt'})"
    )
    return config


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
        config = _apply_story_allocation(config, state, log_verbose=_log_verbose)
        stamp_complexity_provenance(state)
        return config

    from theforge.assignment import (  # noqa: I001, PLC0415
        _normalize_complexity as _norm_complexity,
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
    from theforge.coordinator.audit_substrate import (  # noqa: PLC0415
        load_observed_cost_cohorts as _load_observed_cost_cohorts,
    )

    _observed_costs, _observed_costs_excluded = _load_observed_cost_cohorts(config.project_root)
    # Both loaders apply the same taint gate over the same audit records, so the
    # two counts describe one set of set-aside runs, not two. Reported as the
    # larger of the pair — summing would double-count the same runs and overstate
    # how much history was discounted.
    _excluded_for_taint = max(_excluded_for_taint, _observed_costs_excluded)

    from theforge.model_profiles_storage import load_profiles as _load_profiles  # noqa: PLC0415

    _profiles_path = config.project_root / ".forge" / "model_profiles.yaml"
    _model_profiles = _load_profiles(_profiles_path)

    # Durable demonstrated-capability record (#2466), written by
    # ``forge check-providers``. Loaded unconditionally — unlike the learning
    # profiles above, a demonstrated absence is a hard eligibility fact about the
    # model, so static routing must honor it too.
    from theforge.model_capabilities import (  # noqa: PLC0415
        capabilities_path as _capabilities_path,
    )
    from theforge.model_capabilities import (
        load_capabilities as _load_capabilities,
    )

    _capability_records = _load_capabilities(_capabilities_path(config.project_root))

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
        _explicit_planner = model_ref_to_profile(
            "plan",
            config.plan.ref,
            # See plan_flow: the plan role names the investigation set rather
            # than borrowing preflight's narrowed one (#2346).
            allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
            phase=PHASE_PLAN,
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
            secrets=config.secrets,
            model_profiles=_model_profiles,
            observed_costs=_observed_costs,
            unhealthy_models=_unhealthy if _unhealthy else None,
            domains=list(state.preflight_domains or []),
            excluded_for_taint=_excluded_for_taint,
            sprint_exploration_budget=explore_budget,
            transport_fallbacks=config.transport_fallbacks,
            capability_records=_capability_records,
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
                capability_records=_capability_records,
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
        from theforge.model_profiles_identity import canonical_id_from_identity  # noqa: PLC0415

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

    # Profile-backed dev pre-promotion (#158) is decided entirely inside
    # assign_models from the recency-weighted profile signal and is already
    # reflected in _decision.rationale / routing_decision below; the router is the
    # single authoritative driver, so preflight keeps no separate promotion cache.
    if state.preflight_degraded:
        _log_verbose(
            f"[adaptive] Complexity: {_norm_complexity(complexity)} "
            "(conservative degraded fallback — preflight produced no founded "
            f"classification: {state.preflight_degraded_reason or 'unknown'})"
        )
    else:
        _log_verbose(f"[adaptive] Complexity: {_norm_complexity(complexity)} (from preflight)")
    for _phase, _rsn in _decision.rationale.items():
        _log_verbose(f"[adaptive] {_phase}: {_rsn}")

    config = _apply_story_allocation(config, state, log_verbose=_log_verbose)
    stamp_complexity_provenance(state)
    return config


def persist_routing_decision(
    config: ForgeConfig,
    state: "CoordinatorState",
    *,
    task_slug: str,
    story_content: str | None = None,
    run_id: str | None = None,
    log_verbose: Callable[[str], None] | None = None,
) -> "Path | None":
    """Write this story's resolved routing decision to disk.

    Called immediately after :func:`_apply_preflight_config` installs the
    decision onto ``config``, so what is recorded is exactly what the phases
    will execute. The record is the only copy that survives a mid-sprint
    process re-exec, which drops the scheduler's in-memory preflight cache
    (#2154).
    """
    from .routing_persistence import (  # noqa: PLC0415
        build_routing_record,
        save_routing_record,
    )

    _log_verbose = log_verbose or (lambda _msg: None)
    record = build_routing_record(
        slug=task_slug,
        state=state,
        review_pool_models=[p.model for p in config.review_pool],
        dev_model=config.dev_profile.model,
        plan_model=config.plan.model,
        story_content=story_content,
        run_id=run_id,
    )
    path = save_routing_record(config.project_root, record)
    if path is not None:
        _log_verbose(
            f"[adaptive] routing decision persisted: {record['code_reviewer_count']} "
            f"reviewer(s) {record['code_reviewers']} → {path}"
        )
    return path


def _pin_pool_to_recorded(
    pool: list[ModelProfile], recorded: list[str]
) -> list[ModelProfile] | None:
    """Return ``pool`` narrowed to the recorded models, or None if impossible.

    Profiles are taken from ``pool`` — the freshly re-derived, router-sized
    pool — never from the static roster, so each seat keeps the budget the
    router sized for it. If any recorded model is absent the caller is told it
    cannot honour the recorded panel rather than being handed a substitute.
    """
    remaining = list(pool)
    pinned: list[ModelProfile] = []
    for model in recorded:
        match = next((p for p in remaining if p.model == model), None)
        if match is None:
            return None
        remaining.remove(match)
        pinned.append(match)
    return pinned


def restore_routing_decision(
    config: ForgeConfig,
    state: "CoordinatorState",
    *,
    task_slug: str,
    story_content: str | None,
    log: Callable[[str], None] | None = None,
    log_verbose: Callable[[str], None] | None = None,
) -> tuple[ForgeConfig, dict]:
    """Recover a persisted routing decision for a story resumed without a cache.

    Returns ``(config, recovery)``. ``recovery`` is also stitched into
    ``state.complexity_routing_audit["routing_recovery"]`` so the audit trail
    always states which panel the phase actually seated and why (convention 6);
    when the routed panel cannot be honoured, that is said out loud rather than
    silently substituted with the ``forge.yaml`` roster.
    """
    from .routing_persistence import (  # noqa: PLC0415
        apply_routing_record_to_state,
        load_routing_record,
        validate_routing_record,
    )

    _log_fn = log or (lambda _msg: None)
    _log_verbose = log_verbose or (lambda _msg: None)

    def _finish(recovery: dict) -> tuple[ForgeConfig, dict]:
        recovery["seated_review_pool"] = [p.model for p in config.review_pool]
        recovery["seated_count"] = len(config.review_pool)
        _audit = dict(state.complexity_routing_audit or {})
        _audit["routing_recovery"] = recovery
        state.complexity_routing_audit = _audit
        return config, recovery

    record = load_routing_record(config.project_root, task_slug)
    if record is None:
        _log_fn(
            f"  ⚠ ROUTING     no persisted routing decision for {task_slug} — "
            f"REVIEW will seat the configured roster "
            f"({len(config.review_pool)} reviewer(s)), not a routed panel"
        )
        return _finish({"status": "unavailable", "reason": "no_record"})

    usable, reason = validate_routing_record(record, story_content=story_content)
    if not usable:
        _log_fn(
            f"  ⚠ ROUTING     persisted routing decision for {task_slug} rejected "
            f"({reason}) — REVIEW will seat the configured roster "
            f"({len(config.review_pool)} reviewer(s))"
        )
        return _finish({"status": "rejected", "reason": reason})

    recorded = [str(m) for m in record.get("code_reviewers") or []]
    apply_routing_record_to_state(state, record)
    config = _apply_preflight_config(
        config, state, log=log, log_verbose=log_verbose, task_slug=task_slug
    )
    derived = [p.model for p in config.review_pool]

    recovery: dict = {
        "status": "recovered",
        "reason": reason,
        "source_run_id": record.get("run_id"),
        "recorded_review_pool": recorded,
        "recorded_count": len(recorded),
        "rederived_review_pool": derived,
        "complexity": record.get("complexity"),
        "complexity_score": record.get("complexity_score"),
    }

    if derived == recorded:
        recovery["reconciliation"] = "rederived_match"
        _log_fn(
            f"  ✓ ROUTING     recovered routed panel for {task_slug}: "
            f"{len(recorded)} reviewer(s) {recorded}"
        )
        return _finish(recovery)

    pinned = _pin_pool_to_recorded(list(config.review_pool), recorded)
    if pinned is not None:
        config = _dc_replace(config, review_pool=pinned)
        recovery["reconciliation"] = "pinned_to_record"
        _log_fn(
            f"  ✓ ROUTING     recovered routed panel for {task_slug}: "
            f"{len(recorded)} reviewer(s) {recorded} "
            f"(re-derivation proposed {len(derived)}: {derived})"
        )
        return _finish(recovery)

    recovery["reconciliation"] = "unhonoured"
    _log_fn(
        f"  ⚠ ROUTING     routed panel for {task_slug} cannot be honoured: "
        f"recorded {len(recorded)} reviewer(s) {recorded} include models absent "
        f"from the re-derived pool {derived} — seating {len(derived)}"
    )
    _log_verbose(
        "[adaptive] routing recovery could not reproduce the recorded panel; "
        "quantities derived from the seat count describe the seated pool"
    )
    return _finish(recovery)
