"""Tests for model preference lists (fallback on quota/not-found errors).

Covers:
- Config parsing: model: scalar, model: list, models: list, fallback_models:
- API runner: first model succeeds (no fallback), first fails with quota (fallback fires),
  all models fail (error surfaces normally)
- CLI runner: CLI succeeds, CLI quota error triggers fallback_models iteration
- Audit trail: model_used and model_config recorded correctly
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.agent_types import AgentResult, ModelUsage
from theforge.config import ModelProfile, TransportFallbackConfig
from theforge.config.profiles import _parse_model_spec, _parse_profile
from theforge.runners.api import _classify_api_model_fallback, run_api_agent
from theforge.runners.cli import _classify_cli_fallback, _classify_cli_fallback_decision

# ── Helpers ────────────────────────────────────────────────────────────


def _make_api_profile(
    model: str = "gpt-4o",
    fallback_models: tuple[str, ...] = (),
    provider: str = "openai",
    allowed_tools: tuple[str, ...] = (),
) -> ModelProfile:
    return ModelProfile(
        name="test",
        provider=provider,
        cli=None,
        model=model,
        fallback_models=fallback_models,
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=allowed_tools,
    )


def _make_cli_profile(
    model: str = "claude-sonnet-4-6",
    fallback_models: tuple[str, ...] = (),
    cli: str = "claude",
) -> ModelProfile:
    return ModelProfile(
        name="test-cli",
        cli=cli,
        provider=None,
        model=model,
        fallback_models=fallback_models,
        budget_usd=1.0,
        timeout_seconds=60,
        allowed_tools=(),
    )


def _success_result(model: str = "gpt-4o") -> AgentResult:
    return AgentResult(
        success=True,
        output="OK",
        session_id=None,
        cost_usd=0.01,
        exit_code=0,
        raw={},
        profile_name="test",
        model_usage=(
            ModelUsage(
                model=model,
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.01,
            ),
        ),
    )


def _quota_fail_result(model: str = "gpt-4o") -> AgentResult:
    return AgentResult(
        success=False,
        output="Provider API error: 429 rate limit exceeded",
        session_id=None,
        cost_usd=None,
        exit_code=1,
        raw={},
        profile_name="test",
    )


def _model_not_found_result(model: str = "gpt-old") -> AgentResult:
    return AgentResult(
        success=False,
        output="Provider API error: model not found: gpt-old",
        session_id=None,
        cost_usd=None,
        exit_code=1,
        raw={},
        profile_name="test",
    )


def _runtime_fail_result() -> AgentResult:
    return AgentResult(
        success=False,
        output="Schema validation error: invalid review YAML",
        session_id=None,
        cost_usd=None,
        exit_code=1,
        raw={},
        profile_name="test",
    )


# ── Config parsing ─────────────────────────────────────────────────────


class TestParseModelSpec:
    """_parse_model_spec normalizes all model YAML forms."""

    def test_scalar_string(self):
        primary, fallbacks = _parse_model_spec({"model": "gpt-4o"}, "default", (), "p")
        assert primary == "gpt-4o"
        assert fallbacks == ()

    def test_list_form_model_key(self):
        primary, fallbacks = _parse_model_spec(
            {"model": ["gpt-4-turbo", "gpt-4o"]}, "default", (), "p"
        )
        assert primary == "gpt-4-turbo"
        assert fallbacks == ("gpt-4o",)

    def test_models_key_takes_precedence(self):
        primary, fallbacks = _parse_model_spec(
            {"models": ["a", "b", "c"], "model": "ignored"}, "default", (), "p"
        )
        assert primary == "a"
        assert fallbacks == ("b", "c")

    def test_fallback_models_key_appended(self):
        primary, fallbacks = _parse_model_spec(
            {"model": "primary", "fallback_models": ["fb1", "fb2"]}, "default", (), "p"
        )
        assert primary == "primary"
        assert fallbacks == ("fb1", "fb2")

    def test_models_list_plus_fallback_models(self):
        primary, fallbacks = _parse_model_spec(
            {"models": ["a", "b"], "fallback_models": ["c"]}, "default", (), "p"
        )
        assert primary == "a"
        assert fallbacks == ("b", "c")

    def test_no_model_uses_default(self):
        primary, fallbacks = _parse_model_spec({}, "default-model", ("df1",), "p")
        assert primary == "default-model"
        assert fallbacks == ("df1",)

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _parse_model_spec({"model": []}, "default", (), "p")

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="string or list"):
            _parse_model_spec({"model": 42}, "default", (), "p")

    def test_fallback_models_not_list_raises(self):
        with pytest.raises(ValueError, match="must be a list"):
            _parse_model_spec({"model": "x", "fallback_models": "not-a-list"}, "default", (), "p")


class TestParseProfileModelList:
    """_parse_profile normalizes model list into ModelProfile."""

    def test_scalar_model_no_fallbacks(self):
        with patch("importlib.import_module"):
            profile = _parse_profile(
                "rev",
                {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "budget_usd": 1.0,
                    "timeout_seconds": 60,
                },
                role="review",
                secrets={"OPENAI_API_KEY": "test"},
            )
        assert profile.model == "gpt-4o"
        assert profile.fallback_models == ()
        assert profile.models == ("gpt-4o",)

    def test_list_model_parsed(self):
        with patch("importlib.import_module"):
            profile = _parse_profile(
                "rev",
                {
                    "provider": "google",
                    "models": ["gemini-preview", "gemini-2.5-pro"],
                    "budget_usd": 1.0,
                    "timeout_seconds": 60,
                },
                role="review",
                secrets={"GOOGLE_API_KEY": "test"},
            )
        assert profile.model == "gemini-preview"
        assert profile.fallback_models == ("gemini-2.5-pro",)
        assert profile.models == ("gemini-preview", "gemini-2.5-pro")

    def test_fallback_models_key_cli_profile(self):
        profile = _parse_profile(
            "dev",
            {
                "cli": "codex",
                "model": "codex",
                "fallback_models": ["gpt-4o"],
                "budget_usd": 10.0,
                "timeout_seconds": 600,
            },
            role="dev",
        )
        assert profile.model == "codex"
        assert profile.fallback_models == ("gpt-4o",)
        assert profile.models == ("codex", "gpt-4o")


# ── ModelProfile.models property ──────────────────────────────────────


class TestModelProfileModelsProperty:
    def test_single_model(self):
        p = _make_api_profile(model="gpt-4o")
        assert p.models == ("gpt-4o",)

    def test_with_fallbacks(self):
        p = _make_api_profile(model="gpt-4o", fallback_models=("gpt-4o-mini",))
        assert p.models == ("gpt-4o", "gpt-4o-mini")

    def test_scalar_and_list_equivalent(self):
        """Single-element list and scalar string are equivalent."""
        scalar = _make_api_profile(model="gpt-4o")
        # A profile with no fallbacks has models == (model,)
        assert scalar.models == ("gpt-4o",)


# ── API runner fallback logic ──────────────────────────────────────────


class TestClassifyApiModelFallback:
    def test_success_returns_none(self):
        assert _classify_api_model_fallback(_success_result()) is None

    def test_quota_error_triggers_fallback(self):
        result = _quota_fail_result()
        reason = _classify_api_model_fallback(result)
        assert reason is not None
        assert "429" in reason or "rate limit" in reason

    def test_model_not_found_triggers_fallback(self):
        result = _model_not_found_result()
        reason = _classify_api_model_fallback(result)
        assert reason is not None

    def test_runtime_error_no_fallback(self):
        assert _classify_api_model_fallback(_runtime_fail_result()) is None

    def test_resource_exhausted_triggers_fallback(self):
        result = AgentResult(
            success=False,
            output="Provider API error: resource_exhausted",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="test",
        )
        assert _classify_api_model_fallback(result) is not None


class TestRunApiAgentPreferenceList:
    """run_api_agent iterates through models in preference order."""

    def test_first_model_succeeds_no_fallback(self, tmp_path):
        """When first model succeeds, no fallback is attempted."""
        profile = _make_api_profile(model="gpt-4o", fallback_models=("gpt-4o-mini",))
        expected = _success_result("gpt-4o")

        call_log: list[str] = []

        def mock_runner(prompt, prof, secrets):
            call_log.append(prof.model)
            return expected

        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": mock_runner}):
            result = run_api_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.success
        assert call_log == ["gpt-4o"]
        # model_config set because list has >1 entry
        assert result.model_config == ("gpt-4o", "gpt-4o-mini")

    def test_quota_error_triggers_fallback_to_second(self, tmp_path):
        """When first model fails with quota, second model is tried."""
        profile = _make_api_profile(model="gpt-4-turbo", fallback_models=("gpt-4o",))
        quota_fail = _quota_fail_result("gpt-4-turbo")
        success = _success_result("gpt-4o")

        call_log: list[str] = []

        def mock_runner(prompt, prof, secrets):
            call_log.append(prof.model)
            if prof.model == "gpt-4-turbo":
                return quota_fail
            return success

        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": mock_runner}):
            result = run_api_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.success
        assert call_log == ["gpt-4-turbo", "gpt-4o"]
        assert result.model_config == ("gpt-4-turbo", "gpt-4o")

    def test_model_not_found_triggers_fallback(self, tmp_path):
        """Model-not-found error triggers fallback to next model."""
        profile = _make_api_profile(model="gpt-old", fallback_models=("gpt-4o",))
        not_found = _model_not_found_result("gpt-old")
        success = _success_result("gpt-4o")

        call_log: list[str] = []

        def mock_runner(prompt, prof, secrets):
            call_log.append(prof.model)
            return not_found if prof.model == "gpt-old" else success

        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": mock_runner}):
            result = run_api_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.success
        assert call_log == ["gpt-old", "gpt-4o"]

    def test_all_models_fail_returns_last_error(self, tmp_path):
        """When all models fail with quota, error surfaces normally."""
        profile = _make_api_profile(model="m1", fallback_models=("m2",))
        fail1 = _quota_fail_result("m1")
        fail2 = _quota_fail_result("m2")

        call_log: list[str] = []

        def mock_runner(prompt, prof, secrets):
            call_log.append(prof.model)
            return fail1 if prof.model == "m1" else fail2

        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": mock_runner}):
            result = run_api_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert not result.success
        assert call_log == ["m1", "m2"]
        assert result.model_config == ("m1", "m2")

    def test_runtime_error_does_not_trigger_fallback(self, tmp_path):
        """Schema/runtime errors do not trigger fallback — they propagate immediately."""
        profile = _make_api_profile(model="gpt-4o", fallback_models=("gpt-4o-mini",))
        runtime_fail = _runtime_fail_result()

        call_log: list[str] = []

        def mock_runner(prompt, prof, secrets):
            call_log.append(prof.model)
            return runtime_fail

        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": mock_runner}):
            result = run_api_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert not result.success
        # Only first model was attempted
        assert call_log == ["gpt-4o"]

    def test_single_model_no_model_config(self, tmp_path):
        """Scalar model (no fallbacks) does not set model_config."""
        profile = _make_api_profile(model="gpt-4o")
        expected = _success_result("gpt-4o")

        with patch.dict(
            "theforge.runners.api.PROVIDER_RUNNERS", {"openai": lambda p, pr, s: expected}
        ):
            result = run_api_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.model_config == ()

    def test_three_model_list_second_succeeds(self, tmp_path):
        """Middle model succeeds after first quota-fails; third is never tried."""
        profile = _make_api_profile(model="m1", fallback_models=("m2", "m3"))
        fail = _quota_fail_result("m1")
        success = _success_result("m2")

        call_log: list[str] = []

        def mock_runner(prompt, prof, secrets):
            call_log.append(prof.model)
            return fail if prof.model == "m1" else success

        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": mock_runner}):
            result = run_api_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.success
        assert call_log == ["m1", "m2"]


# ── CLI runner fallback_models ─────────────────────────────────────────


class TestCliClassifyFallback:
    """_classify_cli_fallback detects quota and model-not-found patterns."""

    def test_quota_pattern(self):
        r = AgentResult(
            success=False,
            output="error: quota exceeded for this billing period",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) is not None

    def test_usage_limit_pattern(self):
        r = AgentResult(
            success=False,
            output="ERROR: You've hit your usage limit. Upgrade to Pro",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) == "matched 'usage limit'"

    def test_gemini_current_quota_pattern(self):
        r = AgentResult(
            success=False,
            output="You exceeded your current quota, please check your plan and billing details.",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) == "matched 'current quota'"

    def test_gemini_daily_quota_limit_pattern(self):
        r = AgentResult(
            success=False,
            output="You have reached your daily gemini-2.5-pro quota limit.",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) == "matched 'quota limit'"

    def test_credit_balance_pattern(self):
        r = AgentResult(
            success=False,
            output="API Error: Credit balance is too low",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) == "matched 'credit balance'"
        decision = _classify_cli_fallback_decision(r)
        assert decision is not None
        assert decision.cli_quota_error_observed is True

    def test_insufficient_credit_pattern(self):
        r = AgentResult(
            success=False,
            output="error: insufficient credit to complete this request",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        decision = _classify_cli_fallback_decision(r)
        assert decision is not None
        assert decision.cli_quota_error_observed is True

    def test_balance_is_too_low_pattern(self):
        r = AgentResult(
            success=False,
            output="your account balance is too low to run this model",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        decision = _classify_cli_fallback_decision(r)
        assert decision is not None
        assert decision.cli_quota_error_observed is True

    def test_model_not_found_pattern(self):
        r = AgentResult(
            success=False,
            output="error: model not found",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) is not None

    def test_success_no_fallback(self):
        r = AgentResult(
            success=True,
            output="done",
            session_id=None,
            cost_usd=None,
            exit_code=0,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) is None

    def test_runtime_error_no_fallback(self):
        r = AgentResult(
            success=False,
            output="TypeError: cannot convert X to Y",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="cli-test",
        )
        assert _classify_cli_fallback(r) is None


class TestCliRunnerFallbackModels:
    """CLI runner tries fallback_models API models on quota failure."""

    def test_cli_succeeds_no_fallback(self, tmp_path):
        """CLI succeeds: fallback_models never tried, model_config and model_used set."""
        profile = _make_cli_profile(fallback_models=("gpt-4o",))
        cli_result = AgentResult(
            success=True,
            output="all good",
            session_id=None,
            cost_usd=0.01,
            exit_code=0,
            raw={},
            profile_name="test-cli",
        )

        with patch("theforge.runners.runner_claude._run_claude", return_value=cli_result):
            from theforge.runners.cli import run_agent

            result = run_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.success
        assert result.output == "all good"
        # model_config set because fallback_models is configured
        assert result.model_config == ("claude-sonnet-4-6", "gpt-4o")
        # model_used set to the CLI model that actually ran
        assert result.model_used == "claude-sonnet-4-6"

    def test_cli_single_model_model_used_set(self, tmp_path):
        """CLI profile without fallback_models still gets model_used set."""
        profile = _make_cli_profile()  # no fallback_models
        cli_result = AgentResult(
            success=True,
            output="done",
            session_id=None,
            cost_usd=0.01,
            exit_code=0,
            raw={},
            profile_name="test-cli",
        )

        with patch("theforge.runners.runner_claude._run_claude", return_value=cli_result):
            from theforge.runners.cli import run_agent

            result = run_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.success
        assert result.model_used == "claude-sonnet-4-6"
        assert result.model_config == ()  # no preference list configured

    def test_cli_quota_fires_fallback_models(self, tmp_path):
        """CLI quota error causes fallback_models API model to be tried."""
        profile = _make_cli_profile(cli="codex", model="codex", fallback_models=("gpt-4o",))
        cli_quota = AgentResult(
            success=False,
            output="Error: 429 rate limit exceeded",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="test-cli",
        )
        api_success = _success_result("gpt-4o")

        api_call_log: list[str] = []

        def mock_api_agent(**kwargs):
            api_call_log.append(kwargs["profile"].model)
            return api_success

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_quota),
            patch("theforge.runners.api.run_api_agent", side_effect=mock_api_agent),
        ):
            from theforge.runners.cli import run_agent

            result = run_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert result.success
        assert api_call_log == ["gpt-4o"]
        # model_config and model_used recorded
        assert result.model_config == ("codex", "gpt-4o")
        assert result.model_used == "gpt-4o"
        assert result.cli_quota_error_observed is True
        assert result.transport_fallback_fired is True
        assert result.transport_used == "api"

    def test_cli_usage_limit_fires_api_fallback(self, tmp_path):
        """Codex usage-limit signature triggers API fallback."""
        profile = _make_cli_profile(cli="codex", model="codex", fallback_models=("gpt-5.4",))
        cli_quota = AgentResult(
            success=False,
            output="ERROR: You've hit your usage limit. Upgrade to Pro",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="test-cli",
        )
        api_success = _success_result("gpt-5.4")

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_quota),
            patch("theforge.runners.api.run_api_agent", return_value=api_success) as mock_api,
        ):
            from theforge.runners.cli import run_agent

            result = run_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        mock_api.assert_called_once()
        assert result.success
        assert result.model_used == "gpt-5.4"
        assert result.cli_quota_error_observed is True
        assert result.transport_fallback_fired is True
        assert result.transport_used == "api"

    def test_cli_all_fallbacks_exhausted_returns_last_api_result(self, tmp_path):
        """When CLI fails and all API fallback models also fail, return last API result."""
        profile = _make_cli_profile(cli="codex", model="codex", fallback_models=("m1", "m2"))
        cli_fail = AgentResult(
            success=False,
            output="Error: 429 rate limit exceeded",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="test-cli",
        )
        m1_fail = AgentResult(
            success=False,
            output="Provider API error: quota exceeded",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="test-cli",
        )
        m2_fail = AgentResult(
            success=False,
            output="Provider API error: resource_exhausted",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="test-cli",
        )

        call_log: list[str] = []

        def mock_api_agent(**kwargs):
            model = kwargs["profile"].model
            call_log.append(model)
            return m1_fail if model == "m1" else m2_fail

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_fail),
            patch("theforge.runners.api.run_api_agent", side_effect=mock_api_agent),
        ):
            from theforge.runners.cli import run_agent

            result = run_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert not result.success
        assert call_log == ["m1", "m2"]
        # Must return last API result (not original CLI error)
        assert "resource_exhausted" in result.output
        assert result.model_used == "m2"
        assert result.model_config == ("codex", "m1", "m2")

    def test_legacy_api_fallback_failure_returned_not_cli_error(self, tmp_path):
        """When legacy api_fallback is configured, fails, and there are no fallback_models,
        the API failure result must be returned — not the original CLI failure."""
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            provider=None,
            model="codex",
            fallback_models=(),
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
            api_fallback=TransportFallbackConfig(provider="openai", model="o4-mini"),
        )
        cli_fail = AgentResult(
            success=False,
            output="Error: 429 rate limit exceeded",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="codex-reviewer",
        )
        api_fail = AgentResult(
            success=False,
            output="openai: 503 service unavailable",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="codex-reviewer",
        )

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_fail),
            patch("theforge.runners.api.run_api_agent", return_value=api_fail),
        ):
            from theforge.runners.cli import run_agent

            result = run_agent(
                prompt="test",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )

        assert not result.success
        # Must return the API failure, not the original CLI failure
        assert "503 service unavailable" in result.output
        assert result.model_used == "o4-mini"


# ── Audit trail ────────────────────────────────────────────────────────


class TestAuditModelConfig:
    """model_used and model_config appear in audit entries when fallback fires."""

    def test_model_config_in_audit_entry(self):
        from theforge.coordinator.audit_render import _agent_entry

        usage = ModelUsage(
            model="gpt-4o",
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.01,
        )
        result = AgentResult(
            success=True,
            output="ok",
            session_id=None,
            cost_usd=0.01,
            exit_code=0,
            raw={},
            profile_name="rev",
            model_usage=(usage,),
            model_config=("gpt-4-turbo", "gpt-4o"),
        )
        entry = _agent_entry(result, "review", "rev", 5.0)
        assert entry["model_used"] == "gpt-4o"
        assert entry["model_config"] == ["gpt-4-turbo", "gpt-4o"]

    def test_no_model_config_when_single_model(self):
        from theforge.coordinator.audit_render import _agent_entry

        usage = ModelUsage(
            model="gpt-4o",
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.01,
        )
        result = AgentResult(
            success=True,
            output="ok",
            session_id=None,
            cost_usd=0.01,
            exit_code=0,
            raw={},
            profile_name="rev",
            model_usage=(usage,),
        )
        entry = _agent_entry(result, "review", "rev", 3.0)
        assert entry["model_used"] == "gpt-4o"
        assert "model_config" not in entry
