"""Regression: an explicitly empty model registry must stay explicit (issue #1355).

Coordinator consumers used ``config.model_registry or None`` before resolving a
registry. Because ``ForgeConfig.model_registry`` is a plain ``dict`` (never
``None`` — ``default_factory=dict``), that idiom collapsed an *empty* registry
``{}`` to ``None``, which the resolution helpers treat as "no registry supplied,
use the built-in default." The result: a directly-constructed / config-boundary
value of ``{}`` silently fell back to the built-in registry instead of being
honored as an intentional empty registry.

These tests pin the corrected contract:
  * ``None`` still selects the built-in default (unchanged).
  * an explicit ``{}`` is honored as-is — ``model_info_view`` returns an empty
    view and ``resolve_agent_spec`` raises ``ValueError`` (fails clearly) rather
    than substituting the built-in registry.
  * the preflight seam propagates the empty registry without collapsing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.models import (
    AGENT_REGISTRY,
    MODEL_REGISTRY,
    model_info_view,
    resolve_agent_spec,
)
from theforge.config.types import PlanConfig
from theforge.coordinator.preflight import _apply_complexity_adaptation

_BUILTIN_KEY = next(iter(AGENT_REGISTRY))


# ── root cause: resolve_agent_spec distinguishes None from {} ──────────


def test_resolve_agent_spec_none_uses_builtin_default():
    """registry=None means 'absent' — resolve against the built-in AGENT_REGISTRY."""
    spec = resolve_agent_spec(_BUILTIN_KEY, registry=None)
    assert spec is AGENT_REGISTRY[_BUILTIN_KEY]


def test_resolve_agent_spec_empty_dict_fails_clearly():
    """An explicit empty registry is honored: every key is unknown, so it raises."""
    with pytest.raises(ValueError, match="Unknown model"):
        resolve_agent_spec(_BUILTIN_KEY, registry={})


def test_model_info_view_none_returns_builtin():
    assert model_info_view(None) is MODEL_REGISTRY


def test_model_info_view_empty_dict_returns_empty_view():
    """An explicit {} yields an empty view — not silently the built-in default."""
    assert model_info_view({}) == {}


# ── preflight seam: empty registry propagates without collapsing ──────


def _make_config(tmp_path: Path, model_registry: dict) -> ForgeConfig:
    dev = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    plan = PlanConfig(cli="claude", model="sonnet", budget_usd=0.5, timeout=600)
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
        review_pool=[dev],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        models=[_BUILTIN_KEY],
        plan=plan,
        plan_model_is_default=True,
        dev_profile_is_default=True,
        review_pool_is_default=True,
        model_registry=model_registry,
    )


def test_complexity_adaptation_empty_registry_does_not_silently_use_builtin(tmp_path):
    """With an explicit empty registry, adaptation fails clearly instead of
    routing off the built-in registry as if the registry were absent."""
    config = _make_config(tmp_path, model_registry={})
    with pytest.raises(ValueError, match="Unknown model"):
        _apply_complexity_adaptation(config, "medium", complexity_score=7)


def test_complexity_adaptation_absent_registry_uses_builtin(tmp_path):
    """A directly-constructed config leaves model_registry as None (absent) — the
    consumer boundary falls back to the built-in registry, as before the fix."""
    config = _make_config(tmp_path, model_registry=None)
    assert config.model_registry is None
    adapted = _apply_complexity_adaptation(config, "medium", complexity_score=7)
    assert adapted.dev_profile.model == AGENT_REGISTRY[_BUILTIN_KEY].model


def test_forge_config_registry_field_default_is_none():
    """The field default is None (absent), not {} — so absence and emptiness
    are distinguishable at the type level (the root of the falsy-collapse bug)."""
    from dataclasses import MISSING, fields

    field = next(f for f in fields(ForgeConfig) if f.name == "model_registry")
    assert field.default is None
    assert field.default_factory is MISSING


def test_complexity_adaptation_populated_registry_routes(tmp_path):
    """Baseline: a populated registry (the production path) resolves normally,
    confirming the fix is a no-op for loaded configs."""
    config = _make_config(tmp_path, model_registry=dict(AGENT_REGISTRY))
    adapted = _apply_complexity_adaptation(config, "medium", complexity_score=7)
    # A single-model pool leaves dev on that model; the call must not raise.
    assert adapted.dev_profile.model == AGENT_REGISTRY[_BUILTIN_KEY].model
