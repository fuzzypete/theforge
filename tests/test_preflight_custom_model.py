"""Regression: custom forge.yaml model overlays must work in coordinator paths.

The bug (issue #1355): preflight `_apply_complexity_adaptation` and the
escalation helpers consulted built-in MODEL_REGISTRY/AGENT_REGISTRY directly,
so a model declared under `models.custom` (which `forge check-config` accepts)
crashed sprint workers with `ValueError: Unknown model 'openai/gpt-5.5': not in
AGENT_REGISTRY` before any productive work was done.

These tests assert that, given a ForgeConfig whose `model_registry` is the
merged built-in + forge.yaml view, complexity adaptation and escalation
succeed for a custom model and the merged registry's source attribution is
preserved through the seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.config import (
    AGENT_REGISTRY,
    DEFAULT_VALIDATION,
    AgentSpec,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    TransportSpec,
    WorkspaceConfig,
)
from theforge.config.models import RoutingPolicy
from theforge.config.types import PlanConfig
from theforge.coordinator.preflight import (
    _apply_complexity_adaptation,
    _build_pool_entries,
    _escalate_dev_model,
    _find_registry_key_for_profile,
)

_CUSTOM_KEY = "openai/gpt-5.5/api"


def _custom_spec() -> AgentSpec:
    return AgentSpec(
        provider="openai",
        model="gpt-5.5",
        transport=TransportSpec(kind="api", runner="openai"),
        routing=RoutingPolicy(tier="strong", capability=9, cost_rank=3, dev_capable=True),
        registry_source="forge.yaml",
        input_cost_per_mtok=5.0,
        output_cost_per_mtok=30.0,
    )


def _merged_registry() -> dict[str, AgentSpec]:
    merged = dict(AGENT_REGISTRY)
    merged[_CUSTOM_KEY] = _custom_spec()
    return merged


def _registry_sources() -> dict[str, str]:
    sources = {key: "builtin" for key in AGENT_REGISTRY}
    sources[_CUSTOM_KEY] = "forge.yaml"
    return sources


def _make_config(tmp_path: Path) -> ForgeConfig:
    """A simple-mode ForgeConfig whose pool includes a custom forge.yaml model."""
    dev = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    custom = ModelProfile(
        name="openai-gpt-5.5",
        cli=None,
        provider="openai",
        model="gpt-5.5",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    plan = PlanConfig.of(cli="claude", model="sonnet", budget_usd=0.5, timeout=600)
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=dev,
        preflight_profile=dev,
        review_pool=[custom],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        models=["anthropic/sonnet/cli", _CUSTOM_KEY],
        plan=plan,
        plan_model_is_default=True,
        dev_profile_is_default=True,
        review_pool_is_default=True,
        model_registry=_merged_registry(),
        model_registry_sources=_registry_sources(),
        custom_models=(_CUSTOM_KEY,),
    )


# ── _build_pool_entries: registry overlay observed ─────────────────────


def test_build_pool_entries_accepts_custom_model_with_merged_registry():
    """Without registry= the custom model raises; with merged registry it resolves."""
    merged = _merged_registry()
    entries = _build_pool_entries(["anthropic/sonnet/cli", _CUSTOM_KEY], registry=merged)
    keys = {key for _, key, _ in entries}
    assert _CUSTOM_KEY in keys


def test_build_pool_entries_without_registry_raises_for_custom_key():
    """Sanity: the bug's failure mode persists when no registry is supplied."""
    with pytest.raises(ValueError, match="Unknown model"):
        _build_pool_entries(["anthropic/sonnet/cli", _CUSTOM_KEY])


# ── _apply_complexity_adaptation: full preflight seam, custom in pool ─


def test_complexity_adaptation_custom_model_does_not_raise(tmp_path):
    config = _make_config(tmp_path)
    adapted = _apply_complexity_adaptation(config, "medium", complexity_score=7)
    # Score 7 routes dev to 'strong'; custom model is the only strong entry.
    assert adapted.dev_profile.model == "gpt-5.5"


def test_complexity_adaptation_preserves_registry_attribution(tmp_path):
    """Audit attribution survives the fix (per spec fix-success criterion)."""
    config = _make_config(tmp_path)
    adapted = _apply_complexity_adaptation(config, "medium", complexity_score=7)
    assert adapted.model_registry_sources[_CUSTOM_KEY] == "forge.yaml"
    assert adapted.model_registry_sources["anthropic/sonnet/cli"] == "builtin"


# ── escalation paths use merged registry ──────────────────────────────


def test_escalate_dev_model_includes_custom_model(tmp_path):
    """A custom 'strong' model is a valid escalation target for a built-in mid model."""
    merged = _merged_registry()
    next_key = _escalate_dev_model(
        "anthropic/sonnet/cli", ["anthropic/sonnet/cli", _CUSTOM_KEY], registry=merged
    )
    assert next_key == _CUSTOM_KEY


def test_find_registry_key_for_profile_resolves_custom_model(tmp_path):
    custom_profile = ModelProfile(
        name="openai-gpt-5.5",
        cli=None,
        provider="openai",
        model="gpt-5.5",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    merged = _merged_registry()
    key = _find_registry_key_for_profile(custom_profile, registry=merged)
    assert key == _CUSTOM_KEY
