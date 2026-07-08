"""Tests for custom model key handling in preflight pool construction."""

from __future__ import annotations

import pytest

from theforge.coordinator.preflight import _build_pool_entries

_CUSTOM_KEY = "openai/gpt-5.5"


def test_build_pool_entries_without_registry_raises_for_custom_key():
    """Sanity: a model key absent from AGENT_REGISTRY raises ValueError."""
    with pytest.raises(ValueError, match="Unknown model"):
        _build_pool_entries(["claude/sonnet", _CUSTOM_KEY])
