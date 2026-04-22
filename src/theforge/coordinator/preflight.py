"""Coordinator preflight parsing, complexity adaptation, and model escalation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

import yaml

from theforge.config import MODEL_REGISTRY, ForgeConfig, ModelInfo, ModelProfile, apply_model_info
from theforge.review import ReviewFinding

if TYPE_CHECKING:
    from .state import CoordinatorState

_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})

_log = logging.getLogger(__name__)

# Tier × complexity routing table (mirrors role_derivation._COMPLEXITY_TIER).
# Not imported from there to avoid coordinator ↔ config coupling.
_PHASE_COMPLEXITY_TIER: dict[str, dict[str, str]] = {
    "dev": {"LOW": "cheap", "MEDIUM": "mid", "HIGH": "strong"},
    "plan": {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"},
    "review": {"LOW": "mid", "MEDIUM": "mid", "HIGH": "strong"},
}

_TIER_TO_RANK: dict[str, int] = {"cheap": 1, "mid": 2, "strong": 3}

_COMPLEXITY_TO_LEVEL: dict[str, str] = {
    "small": "LOW",
    "medium": "MEDIUM",
    "large": "HIGH",
}


def _build_pool_entries(model_keys: list[str]) -> list[tuple[int, str, ModelInfo]]:
    """Build sorted (cost_rank, registry_key, ModelInfo) list from models."""
    from theforge.config.models import _resolve_model_info  # noqa: PLC0415

    entries: list[tuple[int, str, ModelInfo]] = []
    for key in model_keys:
        info: ModelInfo = MODEL_REGISTRY.get(key) or _resolve_model_info(key)
        entries.append((info.cost_rank, key, info))
    entries.sort(key=lambda x: (x[0], -x[2].capability))
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


def _parse_preflight_bundle_candidate(output: str) -> bool:
    """Extract bundle_candidate from preflight agent output. Defaults to False."""
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
            return bool(parsed.get("bundle_candidate", False))
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
    satisfied (bool), evidence (str).

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
                    "satisfied": bool(entry.get("satisfied", False)),
                    "evidence": str(entry.get("evidence", "")),
                }
            )
        return result
    except yaml.YAMLError:
        return []


def _find_registry_info_for_profile(profile: ModelProfile) -> tuple[int, int]:
    """Return (cost_rank, capability) for a profile using the model registry.

    CLI profiles match by cli+model. API profiles match by provider+model against
    the registry key because registry entries are keyed by provider/model while
    storing the corresponding CLI transport.

    Falls back to (2, 5) for unknown models.
    """
    registry_key = _find_registry_key_for_profile(profile)
    if registry_key is None:
        return 2, 5
    info = MODEL_REGISTRY[registry_key]
    return info.cost_rank, info.capability


def _find_registry_key_for_profile(profile: ModelProfile) -> str | None:
    """Return the MODEL_REGISTRY key for a profile, or None if unknown.

    Matches by TransportSpec (single source of dispatch truth) plus model name.
    Falls back to cli/provider matching for profiles without an explicit
    transport.
    """
    profile_transport = profile.transport
    for key, info in MODEL_REGISTRY.items():
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
    >=60% token overlap) or when they recur in the same file.
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
    """Return True when two P1 findings should be treated as the same persistent issue."""
    if (
        current.file
        and previous.file
        and current.file != "unknown"
        and previous.file != "unknown"
        and current.file == previous.file
    ):
        return True

    if current.description in previous.description or previous.description in current.description:
        return True

    curr_tokens = set(current.description.lower().split())
    prev_tokens = set(previous.description.lower().split())
    if curr_tokens and prev_tokens:
        overlap = len(curr_tokens & prev_tokens) / max(len(curr_tokens), len(prev_tokens))
        if overlap >= 0.6:
            return True

    return False


def _escalate_dev_model(
    current_model: str,
    available_models: list[str],
) -> str | None:
    """Return the next higher-capability dev-capable model, or None.

    Selects the lowest-capability model that is still higher than current
    and has dev_capable=True in MODEL_REGISTRY.
    """
    current_info = MODEL_REGISTRY.get(current_model)
    if current_info is None:
        return None

    candidates = [
        (key, MODEL_REGISTRY[key])
        for key in available_models
        if key in MODEL_REGISTRY
        and MODEL_REGISTRY[key].dev_capable
        and MODEL_REGISTRY[key].capability > current_info.capability
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1].capability)
    return candidates[0][0]


def _apply_complexity_adaptation(config: ForgeConfig, complexity: str) -> ForgeConfig:
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

    pool_entries = _build_pool_entries(config.models)
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
        target_dev_rank = _TIER_TO_RANK[_PHASE_COMPLEXITY_TIER["dev"][norm]]
        target_dev_info = _pick_pool_entry_by_rank(dev_pool, target_dev_rank)
        if target_dev_info.model != config.dev_profile.model:
            new_dev = apply_model_info(config.dev_profile, target_dev_info)
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

        mid_strong = [p for p in review_candidates if _find_registry_info_for_profile(p)[0] >= 2]

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
                    _find_registry_info_for_profile(p)[0],
                    -_find_registry_info_for_profile(p)[1],
                ),
            )
            new_config = _dc_replace(new_config, review_pool=[single], synthesis_profile=None)
        else:
            # MEDIUM/HIGH → all mid/strong reviewers + synthesis
            review_broader = mid_strong if mid_strong else review_candidates
            synthesis = config.synthesis_profile
            if synthesis is None:
                synth_candidates = review_broader or [new_config.dev_profile]
                strongest = max(
                    synth_candidates, key=lambda p: _find_registry_info_for_profile(p)[1]
                )
                synth_budget = max(config.dev_profile.budget_usd * 0.02, 1.0)
                synthesis = _dc_replace(strongest, name="synthesis", budget_usd=synth_budget)
            new_config = _dc_replace(
                new_config, review_pool=review_broader, synthesis_profile=synthesis
            )

    return new_config


def _apply_preflight_config(
    config: ForgeConfig,
    state: "CoordinatorState",
    *,
    log: Callable[[str], None] | None = None,
    log_verbose: Callable[[str], None] | None = None,
) -> ForgeConfig:
    """Apply complexity-driven config updates using values already stored on state."""
    complexity = state.preflight_complexity or "medium"
    _log = log or (lambda _msg: None)
    _log_verbose = log_verbose or (lambda _msg: None)

    if config.models is not None:
        _config_before = config
        config = _apply_complexity_adaptation(config, complexity)
        _dev_changed = config.dev_profile.model != _config_before.dev_profile.model
        _plan_changed = config.plan.model != _config_before.plan.model
        _review_changed = [p.model for p in config.review_pool] != [
            p.model for p in _config_before.review_pool
        ]
        if _dev_changed or _plan_changed or _review_changed:
            state.complexity_routing_audit = {
                "complexity": complexity,
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
        load_escalation_history as _load_esc_history,
    )
    from theforge.config import (  # noqa: I001, PLC0415
        DEFAULT_DEV_PROFILE as _DEF_DEV,
        DEFAULT_PREFLIGHT_PROFILE as _DEF_PRE,
    )

    _history_path = config.project_root / ".forge" / "assignment_history.yaml"
    _esc_history = _load_esc_history(_history_path)

    _explicit: dict[str, object] = {}
    _explicit_roles: set[str] = set()
    if config.models is None:
        if config.dev_profile is not _DEF_DEV:
            _explicit["dev"] = config.dev_profile
            _explicit_roles.add("dev")
        if config.preflight_profile is not _DEF_PRE:
            _explicit["preflight"] = config.preflight_profile
            _explicit_roles.add("preflight")
        if config.review_pool and not config.review_pool_is_default:
            _explicit_roles.add("review_pool")
        if not config.plan_model_is_default:
            _explicit_roles.add("planner")
        if config.plan_agent_review.enabled and config.plan_agent_review.profiles:
            _explicit_roles.add("plan_agent_review")

    _decision = _assign_models(
        config.agents,
        config.assignment,
        complexity,
        _esc_history,
        _explicit if _explicit else None,
        state.sprint_promotions,
        config.secrets,
    )

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

    _dev_base_tier = _PHASE_TIER["dev"][_norm_complexity(complexity)]
    _dev_agent = _pick_agt(config.agents, _dev_base_tier, config.secrets)
    _dev_name = _dev_agent.name if _dev_agent else ""
    if _dev_name and "dev" not in _explicit and complexity not in state.sprint_promotions:
        _prom = _chk_prom(
            _norm_complexity(complexity),
            _dev_name,
            _esc_history,
            state.sprint_promotions,
        )
        if _prom is not None:
            _promoted_tier = _prom_tier(_dev_base_tier)
            state.sprint_promotions[_norm_complexity(complexity)] = _promoted_tier
            _log_verbose(
                f"[adaptive] {_norm_complexity(complexity)} dev promoted "
                f"{_dev_name} -> tier {_promoted_tier} (sticky for sprint)"
            )

    _log_verbose(f"[adaptive] Complexity: {_norm_complexity(complexity)} (from preflight)")
    for _phase, _rsn in _decision.rationale.items():
        _log_verbose(f"[adaptive] {_phase}: {_rsn}")

    return config
