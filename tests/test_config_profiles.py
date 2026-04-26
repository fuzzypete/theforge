"""Focused tests for default phase tool exposure."""

from theforge.config.defaults import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
)


def test_dev_profile_includes_webfetch() -> None:
    assert "WebFetch" in DEFAULT_DEV_PROFILE.allowed_tools


def test_review_and_preflight_profiles_exclude_webfetch() -> None:
    assert "WebFetch" not in DEFAULT_REVIEW_PROFILE.allowed_tools
    assert "WebFetch" not in DEFAULT_PREFLIGHT_PROFILE.allowed_tools


def test_no_default_phase_profile_includes_websearch() -> None:
    assert "WebSearch" not in DEFAULT_DEV_PROFILE.allowed_tools
    assert "WebSearch" not in DEFAULT_REVIEW_PROFILE.allowed_tools
    assert "WebSearch" not in DEFAULT_PREFLIGHT_PROFILE.allowed_tools
