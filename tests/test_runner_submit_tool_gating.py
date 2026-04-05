"""Tests that submit tools and finalizers are only wired for review phases.

Gating is based on profile.phase, not profile.name, so adaptive assignment
that preserves agent-specific names (e.g. "gemini-pro", "sonnet") still
correctly excludes submit tools for dev/preflight runs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.config import ModelProfile
from theforge.runners.schema_utils import (
    _NO_SUBMIT_PHASES,
    SUBMIT_PLAN_REVIEW,
    SUBMIT_REVIEW,
    noop_finalizer,
)


def _make_profile(
    name: str,
    provider: str = "openai",
    model: str = "gpt-4o",
    phase: str | None = None,
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        cli=None,
        model=model,
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("read_file", "bash"),
        phase=phase,
    )


def _capture(
    runner_fn, profile: ModelProfile, tmp_path: Path, extra_patches=()
) -> tuple[list[dict], object]:
    """Return (tool_schemas, finalizer) from a runner without executing the loop.

    tool_schemas is passed to manager.run(); finalizer is passed to AgentLoopManager().
    """
    with patch("theforge.runners.api.AgentLoopManager") as mock_mgr:
        mock_mgr.return_value.run.side_effect = RuntimeError("stop")
        ctx_patches = [patch(p, return_value=object()) for p in extra_patches]
        for p in ctx_patches:
            p.start()
        try:
            runner_fn("prompt", profile, tmp_path)
        except RuntimeError:
            pass
        finally:
            for p in ctx_patches:
                p.stop()
        finalizer = mock_mgr.call_args[1].get("finalizer") if mock_mgr.call_args else None
        run_call = mock_mgr.return_value.run.call_args
        schemas = run_call[1].get("tool_schemas", []) if run_call else []
        return schemas, finalizer


def _captured_openai(profile: ModelProfile, tmp_path: Path) -> tuple[list[dict], object]:
    from theforge.runners.api import _run_loop_openai

    return _capture(
        _run_loop_openai,
        profile,
        tmp_path,
        extra_patches=[
            "theforge.runners.api._make_openai_chat_adapter",
            "theforge.runners.api._make_openai_chat_finalizer",
        ],
    )


def _captured_anthropic(profile: ModelProfile, tmp_path: Path) -> tuple[list[dict], object]:
    from theforge.runners.api import _run_loop_anthropic

    return _capture(
        _run_loop_anthropic,
        profile,
        tmp_path,
        extra_patches=[
            "theforge.runners.api._make_anthropic_adapter",
            "theforge.runners.api._make_anthropic_finalizer",
        ],
    )


def _captured_google(profile: ModelProfile, tmp_path: Path) -> tuple[list[dict], object]:
    from theforge.runners.api import _run_loop_google

    return _capture(
        _run_loop_google,
        profile,
        tmp_path,
        extra_patches=[
            "theforge.runners.api._make_google_adapter",
            "theforge.runners.api._make_google_finalizer",
        ],
    )


def _captured_deepseek(profile: ModelProfile, tmp_path: Path) -> tuple[list[dict], object]:
    from theforge.runners.api import _run_loop_deepseek

    return _capture(
        _run_loop_deepseek,
        profile,
        tmp_path,
        extra_patches=[
            "theforge.runners.adapters.deepseek._deepseek_client",
            "theforge.runners.api._make_openai_chat_adapter",
            "theforge.runners.api._make_deepseek_finalizer",
        ],
    )


def _tool_names(schemas: list[dict]) -> set[str]:
    names = set()
    for s in schemas:
        if "function" in s:
            names.add(s["function"]["name"])
        elif "name" in s:
            names.add(s["name"])
    return names


class TestNoSubmitPhases:
    def test_set_contains_preflight_and_dev(self):
        assert "preflight" in _NO_SUBMIT_PHASES
        assert "dev" in _NO_SUBMIT_PHASES

    def test_reviewer_not_in_set(self):
        assert "openai-reviewer" not in _NO_SUBMIT_PHASES
        assert "gemini-reviewer" not in _NO_SUBMIT_PHASES
        assert "openai-plan-reviewer" not in _NO_SUBMIT_PHASES

    def test_none_phase_not_in_set(self):
        # phase=None means unknown — treated as review (safe default)
        assert None not in _NO_SUBMIT_PHASES


class TestOpenAISubmitGating:
    def test_preflight_phase_gets_no_submit_tools(self, tmp_path):
        # phase="preflight" regardless of name — adaptive agent named "gemini-pro"
        profile = _make_profile("gemini-pro", phase="preflight")
        schemas, finalizer = _captured_openai(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names
        assert finalizer is noop_finalizer

    def test_dev_phase_gets_no_submit_tools(self, tmp_path):
        # phase="dev" regardless of name — adaptive agent named "sonnet"
        profile = _make_profile("sonnet", phase="dev")
        schemas, finalizer = _captured_openai(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names
        assert finalizer is noop_finalizer

    def test_reviewer_gets_submit_tools_and_real_finalizer(self, tmp_path):
        profile = _make_profile("openai-reviewer", phase="review")
        schemas, finalizer = _captured_openai(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW in names
        assert SUBMIT_PLAN_REVIEW in names
        assert finalizer is not noop_finalizer

    def test_none_phase_gets_submit_tools(self, tmp_path):
        # Unset phase defaults to review-like behavior (safe)
        profile = _make_profile("unknown", phase=None)
        schemas, _ = _captured_openai(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW in names


class TestAnthropicSubmitGating:
    def test_preflight_phase_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("claude-sonnet-4-6", provider="anthropic", phase="preflight")
        schemas, finalizer = _captured_anthropic(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names
        assert finalizer is noop_finalizer

    def test_dev_phase_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("claude-sonnet-4-6", provider="anthropic", phase="dev")
        schemas, finalizer = _captured_anthropic(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW not in names
        assert finalizer is noop_finalizer

    def test_reviewer_gets_submit_tools(self, tmp_path):
        profile = _make_profile("anthropic-reviewer", provider="anthropic", phase="review")
        schemas, finalizer = _captured_anthropic(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW in names
        assert finalizer is not noop_finalizer


class TestGoogleSubmitGating:
    def test_preflight_phase_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("gemini-2.5-pro", provider="google", phase="preflight")
        schemas, finalizer = _captured_google(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names
        assert finalizer is noop_finalizer

    def test_reviewer_gets_submit_tools(self, tmp_path):
        profile = _make_profile("gemini-reviewer", provider="google", phase="review")
        schemas, finalizer = _captured_google(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW in names
        assert finalizer is not noop_finalizer


class TestDeepSeekSubmitGating:
    def test_dev_phase_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("deepseek-reasoner", provider="deepseek", phase="dev")
        schemas, finalizer = _captured_deepseek(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW not in names
        assert finalizer is noop_finalizer

    def test_plan_reviewer_gets_submit_tools(self, tmp_path):
        profile = _make_profile("deepseek-plan-reviewer", provider="deepseek", phase="plan_review")
        schemas, finalizer = _captured_deepseek(profile, tmp_path)
        names = _tool_names(schemas)
        assert SUBMIT_REVIEW in names
        assert finalizer is not noop_finalizer


class TestProviderFallbackPreservesPhase:
    """_apply_provider_fallback must carry phase through to the rebuilt ModelProfile."""

    def test_dev_phase_preserved_after_fallback(self):
        from theforge.config.profiles import _apply_provider_fallback
        from theforge.config.types import ApiFallbackConfig

        dev_profile = _make_profile("sonnet", provider=None, phase="dev")
        dev_profile = ModelProfile(
            name="sonnet",
            cli="claude",
            provider=None,
            model="claude-sonnet-4-6",
            budget_usd=5.0,
            timeout_seconds=600,
            allowed_tools=("read_file", "bash"),
            phase="dev",
        )
        fallbacks = {
            "anthropic": ApiFallbackConfig(provider="anthropic", model="claude-sonnet-4-6")
        }
        result = _apply_provider_fallback(dev_profile, fallbacks)
        assert result.phase == "dev"

    def test_preflight_phase_preserved_after_fallback(self):
        from theforge.config.profiles import _apply_provider_fallback
        from theforge.config.types import ApiFallbackConfig

        preflight_profile = ModelProfile(
            name="codex-preflight",
            cli="codex",
            provider=None,
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("read_file", "bash"),
            phase="preflight",
        )
        fallbacks = {"openai": ApiFallbackConfig(provider="openai", model="o4-mini")}
        result = _apply_provider_fallback(preflight_profile, fallbacks)
        assert result.phase == "preflight"
        # Verify the fallback path would not accidentally enable submit tools
        assert result.phase in {"preflight", "dev"}
