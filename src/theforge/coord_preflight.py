"""Coordinator preflight parsing, complexity adaptation, and model escalation."""

from __future__ import annotations

from dataclasses import replace as _dc_replace

import yaml

from .config import MODEL_REGISTRY, ForgeConfig, ModelProfile
from .review import ReviewFinding

_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})
_VALID_COMPLEXITIES = frozenset({"small", "medium", "large", "low", "high"})
_VALID_DOMAINS = frozenset(
    {
        "frontend-layout",
        "frontend-state",
        "backend-api",
        "backend-data",
        "concurrent",
        "refactor",
        "test",
        "docs",
        "general",
    }
)


def _extract_preflight_yaml(output: str) -> dict | None:
    """Parse the YAML body from a preflight response, or return None."""
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
        return None

    return parsed if isinstance(parsed, dict) else None


def _complexity_score_to_tier(score: int) -> str:
    """Map a 1-10 score onto the legacy small/medium/large buckets."""
    if score <= 3:
        return "small"
    if score <= 6:
        return "medium"
    return "large"


def _normalize_complexity_score(raw: object) -> int:
    """Normalize legacy or numeric complexity values into the 1-10 range."""
    legacy_map = {
        "small": 3,
        "low": 3,
        "medium": 5,
        "med": 5,
        "large": 8,
        "high": 8,
    }
    if isinstance(raw, int):
        return min(max(raw, 1), 10)
    if isinstance(raw, float):
        return min(max(int(raw), 1), 10)

    value = str(raw).strip().lower()
    if value in legacy_map:
        return legacy_map[value]
    try:
        return min(max(int(value), 1), 10)
    except ValueError:
        return 5


def _parse_preflight_verdict(output: str) -> tuple[str, str]:
    """Extract verdict and reason from preflight agent output.

    Returns (verdict, reason). If parsing fails, returns ("PROCEED", reason)
    to avoid blocking on a broken preflight — it's cheaper to try DEV than
    to stall.
    """
    parsed = _extract_preflight_yaml(output)
    if parsed is None:
        return "PROCEED", f"Failed to parse preflight YAML; proceeding anyway. Raw: {output[:200]}"

    verdict = str(parsed.get("verdict", "PROCEED")).upper()
    reason = str(parsed.get("reason", "(no reason provided)"))

    if verdict not in _VALID_PREFLIGHT_VERDICTS:
        return "PROCEED", f"Unknown preflight verdict {verdict!r}; proceeding anyway. {reason}"

    return verdict, reason


def _parse_preflight_warnings(output: str) -> list[str]:
    """Extract warnings list from preflight agent output. Returns [] if absent."""
    parsed = _extract_preflight_yaml(output)
    if parsed is not None:
        raw = parsed.get("warnings", [])
        if isinstance(raw, list):
            return [str(w) for w in raw if w]

    return []


def _parse_preflight_complexity_score(output: str) -> int:
    """Extract the numeric complexity score. Defaults to 5 if absent or invalid."""
    parsed = _extract_preflight_yaml(output)
    if parsed is None:
        return 5
    return _normalize_complexity_score(parsed.get("complexity", 5))


def _parse_preflight_complexity(output: str) -> str:
    """Extract legacy small/medium/large complexity tier from preflight output."""
    return _complexity_score_to_tier(_parse_preflight_complexity_score(output))


def _parse_preflight_domain(output: str) -> str:
    """Extract the domain tag from preflight output. Defaults to general."""
    parsed = _extract_preflight_yaml(output)
    if parsed is None:
        return "general"

    raw = str(parsed.get("domain", "general")).strip().lower()
    if raw in _VALID_DOMAINS:
        return raw
    return "general"


def _find_registry_info_for_profile(profile: ModelProfile) -> tuple[int, int]:
    """Return (cost_rank, capability) for a profile using the model registry.

    Falls back to (2, 5) for unknown models.
    """
    for info in MODEL_REGISTRY.values():
        if info.cli == profile.cli and info.model == profile.model:
            return info.cost_rank, info.capability
    return 2, 5


def _find_registry_key_for_profile(profile: ModelProfile) -> str | None:
    """Return the MODEL_REGISTRY key for a profile, or None if unknown."""
    for key, info in MODEL_REGISTRY.items():
        if info.cli == profile.cli and info.model == profile.model:
            return key
    return None


def _has_persistent_p1(
    current_findings: list[ReviewFinding],
    previous_findings: list[ReviewFinding],
) -> bool:
    """Return True if any P1 appears in both current and previous cycles.

    Matches on description similarity alone (substring containment or
    >=60% token overlap) regardless of file.
    """
    current_p1s = [f for f in current_findings if f.severity == "P1"]
    previous_p1s = [f for f in previous_findings if f.severity == "P1"]

    if not current_p1s or not previous_p1s:
        return False

    for curr in current_p1s:
        for prev in previous_p1s:
            # Substring containment
            if curr.description in prev.description or prev.description in curr.description:
                return True
            # Token overlap >= 60%
            curr_tokens = set(curr.description.lower().split())
            prev_tokens = set(prev.description.lower().split())
            if curr_tokens and prev_tokens:
                overlap = len(curr_tokens & prev_tokens) / max(len(curr_tokens), len(prev_tokens))
                if overlap >= 0.6:
                    return True

    return False


def _persistent_p1_descriptions(
    current_findings: list[ReviewFinding],
    previous_findings: list[ReviewFinding],
) -> list[str]:
    """Return description strings of current P1 findings that match previous P1 findings.

    Uses the same matching logic as _has_persistent_p1 (substring containment or
    >=60% token overlap). Returns matched current descriptions, truncated to 200 chars.
    """
    current_p1s = [f for f in current_findings if f.severity == "P1"]
    previous_p1s = [f for f in previous_findings if f.severity == "P1"]

    if not current_p1s or not previous_p1s:
        return []

    matched: list[str] = []
    for curr in current_p1s:
        for prev in previous_p1s:
            if curr.description in prev.description or prev.description in curr.description:
                matched.append(curr.description[:200])
                break
            curr_tokens = set(curr.description.lower().split())
            prev_tokens = set(prev.description.lower().split())
            if curr_tokens and prev_tokens:
                overlap = len(curr_tokens & prev_tokens) / max(len(curr_tokens), len(prev_tokens))
                if overlap >= 0.6:
                    matched.append(curr.description[:200])
                    break

    return matched


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
