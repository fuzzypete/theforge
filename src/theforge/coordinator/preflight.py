"""Coordinator preflight parsing, complexity adaptation, and model escalation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

import yaml

from theforge.config import MODEL_REGISTRY, ForgeConfig, ModelProfile
from theforge.review import ReviewFinding

if TYPE_CHECKING:
    from .state import CoordinatorState

_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})


def _parse_preflight_verdict(output: str) -> tuple[str, str]:
    """Extract verdict and reason from preflight agent output.

    Returns (verdict, reason). Parse failures and invalid verdicts are treated
    as BLOCKED because downstream classifications from a confused preflight are
    unreliable.
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
        return "BLOCKED", f"Failed to parse preflight YAML; blocking. Raw: {output[:200]}"

    if not isinstance(parsed, dict):
        return "BLOCKED", "Preflight output is not a dict; blocking."

    verdict = str(parsed.get("verdict", "PROCEED")).upper()
    reason = str(parsed.get("reason", "(no reason provided)"))

    if verdict not in _VALID_PREFLIGHT_VERDICTS:
        return (
            "BLOCKED",
            "Unknown preflight verdict "
            f"{verdict!r}; escalating because preflight output is invalid. {reason}",
        )

    return verdict, reason


_VALID_COMPLEXITIES = frozenset({"small", "medium", "large"})

_VALID_SUFFICIENCIES = frozenset({"implementation_ready", "needs_planning"})

_VALID_WORK_TYPES = frozenset({"feature", "refactor", "mechanical", "bug"})


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
    """Return the MODEL_REGISTRY key for a profile, or None if unknown."""
    for key, info in MODEL_REGISTRY.items():
        if profile.cli is not None:
            if info.cli == profile.cli and info.model == profile.model:
                return key
            continue
        if profile.provider is not None and key == f"{profile.provider}/{profile.model}":
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
    if current.file and previous.file and current.file == previous.file:
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
    """Adjust model assignments based on complexity signal.

    Only applies when smart_config_models is set. Explicit profiles are unchanged.
    - small: single cheapest reviewer, skip synthesis
    - medium: no change (auto-assigned defaults)
    - large: upgrade dev to strongest available model
    """
    if config.smart_config_models is None or complexity == "medium":
        return config

    if complexity == "small":
        if len(config.review_pool) <= 1:
            return _dc_replace(config, synthesis_profile=None)
        cheapest = min(
            config.review_pool,
            key=lambda p: (
                _find_registry_info_for_profile(p)[0],
                -_find_registry_info_for_profile(p)[1],
            ),
        )
        return _dc_replace(config, review_pool=[cheapest], synthesis_profile=None)

    if complexity == "large":
        # Find strongest model across all profiles
        candidates: list[ModelProfile] = list(config.review_pool) + [config.dev_profile]
        if config.synthesis_profile is not None:
            candidates.append(config.synthesis_profile)
        strongest = max(candidates, key=lambda p: _find_registry_info_for_profile(p)[1])
        new_dev = _dc_replace(config.dev_profile, cli=strongest.cli, model=strongest.model)
        # Spec: large complexity always runs synthesis; materialize it if absent
        synthesis = config.synthesis_profile
        if synthesis is None:
            # Derive a synthesis budget as 2% of dev budget (min $1)
            synth_budget = max(config.dev_profile.budget_usd * 0.02, 1.0)
            synth_base = config.review_pool[0]
            synthesis = _dc_replace(
                synth_base,
                name="synthesis",
                cli=strongest.cli,
                model=strongest.model,
                budget_usd=synth_budget,
            )
        if (
            new_dev.cli == config.dev_profile.cli
            and new_dev.model == config.dev_profile.model
            and synthesis is config.synthesis_profile
        ):
            return config  # already using strongest, synthesis unchanged
        return _dc_replace(config, dev_profile=new_dev, synthesis_profile=synthesis)

    return config


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

    if config.smart_config_models is not None:
        config = _apply_complexity_adaptation(config, complexity)

    if not (config.assignment.enabled and config.agents):
        return config

    from .assignment import PHASE_TIER as _PHASE_TIER  # noqa: PLC0415
    from .assignment import _check_promotion as _chk_prom  # noqa: PLC0415
    from .assignment import _normalize_complexity as _norm_complexity  # noqa: PLC0415
    from .assignment import _pick_agent as _pick_agt  # noqa: PLC0415
    from .assignment import _promote_tier as _prom_tier  # noqa: PLC0415
    from .assignment import assign_models as _assign_models  # noqa: PLC0415
    from .assignment import load_escalation_history as _load_esc_history  # noqa: PLC0415
    from .config import DEFAULT_DEV_PROFILE as _DEF_DEV  # noqa: PLC0415
    from .config import DEFAULT_PREFLIGHT_PROFILE as _DEF_PRE  # noqa: PLC0415

    _history_path = config.project_root / ".forge" / "assignment_history.yaml"
    _esc_history = _load_esc_history(_history_path)

    _explicit: dict[str, object] = {}
    _explicit_roles: set[str] = set()
    if config.smart_config_models is None:
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
