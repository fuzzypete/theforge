"""Coordinator preflight parsing, complexity adaptation, and model escalation."""

from __future__ import annotations

from dataclasses import replace as _dc_replace
from pathlib import Path

import yaml

from .config import MODEL_REGISTRY, ForgeConfig, ModelProfile
from .review import ReviewFinding
from .task import TaskSpec

_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})


def _load_file_scope_contents(task: TaskSpec, project_root: Path) -> dict[str, str]:
    """Read current contents of files in task.file_scope.

    Returns a dict of {relative_path: content}. Missing files are
    silently skipped (the preflight agent will note their absence).
    """
    contents: dict[str, str] = {}
    for rel_path in task.file_scope:
        full_path = project_root / rel_path
        if full_path.is_file():
            try:
                contents[rel_path] = full_path.read_text(encoding="utf-8")
            except OSError:
                pass
    return contents


def _parse_preflight_verdict(output: str) -> tuple[str, str]:
    """Extract verdict and reason from preflight agent output.

    Returns (verdict, reason). If parsing fails, returns ("PROCEED", reason)
    to avoid blocking on a broken preflight — it's cheaper to try DEV than
    to stall.
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
        return "PROCEED", f"Failed to parse preflight YAML; proceeding anyway. Raw: {output[:200]}"

    if not isinstance(parsed, dict):
        return "PROCEED", "Preflight output is not a dict; proceeding anyway."

    verdict = str(parsed.get("verdict", "PROCEED")).upper()
    reason = str(parsed.get("reason", "(no reason provided)"))

    if verdict not in _VALID_PREFLIGHT_VERDICTS:
        return "PROCEED", f"Unknown preflight verdict {verdict!r}; proceeding anyway. {reason}"

    return verdict, reason


_VALID_COMPLEXITIES = frozenset({"small", "medium", "large"})


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
