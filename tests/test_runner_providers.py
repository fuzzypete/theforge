"""Tests for provider-specific runners: Codex, Gemini, secrets injection, result logging."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.agent_types import AgentResult
from theforge.config import ModelProfile, TransportFallbackConfig
from theforge.log_level import LogLevel, set_log_level
from theforge.runners import log_agent_result, run_agent, runner_codex

# Codex spawn seam patched by the tests below.
_CODEX_RUN_TARGET = "theforge.runners.runner_codex.process_group.run_in_process_group"
# Gemini's spawn seam. Group-isolated like codex's since #2309 — a bare
# subprocess.run left the npm→node→gemini tree outside every teardown path.
_GEMINI_RUN_TARGET = "theforge.runners.runner_gemini.process_group.run_in_process_group"


def _make_stream_mock(lines: list[str], returncode: int = 0, stderr: str = "") -> MagicMock:
    """Return a mock Popen object whose stdout yields the given JSONL lines."""
    mock_proc = MagicMock()
    mock_proc.stdout = iter(lines)
    mock_proc.stdin = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = stderr
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = returncode
    return mock_proc


def _result_line(**fields: object) -> str:
    """Build a stream-json result line (type=result + caller-supplied fields)."""
    return json.dumps({"type": "result", **fields}) + "\n"


class TestRunCodex:
    """Test Codex CLI subprocess invocation."""

    def test_codex_success(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Code looks good.", stderr=""
        )
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc):
            result = run_agent(
                prompt="review this code",
                profile=codex_profile,
                working_dir=tmp_path,
            )

        assert result.success is True
        assert result.output == "Code looks good."
        assert result.session_id is None
        assert result.cost_usd is None
        assert result.exit_code == 0
        assert result.profile_name == "codex-reviewer"

    def test_codex_timeout(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            _CODEX_RUN_TARGET,
            side_effect=subprocess.TimeoutExpired(cmd="npx", timeout=300),
        ):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.success is False
        assert "TIMEOUT" in result.output
        assert "300" in result.output
        assert result.exit_code == -1

    def test_codex_not_found(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            _CODEX_RUN_TARGET,
            side_effect=FileNotFoundError(),
        ):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.success is False
        assert "not found" in result.output
        assert result.exit_code == -1

    def test_codex_command_structure(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=codex_profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "npx" in cmd
        assert runner_codex.CODEX_PACKAGE in cmd
        assert "exec" in cmd
        # --full-auto was removed in codex 0.147.0. It was also redundant here:
        # build_argv sets --sandbox from the profile a few lines later, and the
        # alias only ever meant --sandbox workspace-write.
        assert "--full-auto" not in cmd
        assert "-m" in cmd
        assert "o4-mini" in cmd
        assert "-C" in cmd
        assert str(tmp_path) in cmd
        assert "-o" in cmd

    def test_codex_output_file_fallback(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        """When output file is empty, falls back to stdout."""
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="fallback stdout", stderr=""
        )
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.output == "fallback stdout"

    def test_codex_nonzero_exit(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="partial work", stderr=""
        )
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.exit_code == 1
        assert result.output == "partial work"
        assert result.cost_usd is None

    def test_codex_empty_output_falls_back_to_stderr(
        self, codex_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="codex error"
        )
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.output == "codex error"

    def test_codex_reasoning_effort_high(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            model="o4-mini",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
            reasoning_effort="high",
        )
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "-c" in cmd
        c_idx = cmd.index("-c")
        assert cmd[c_idx + 1] == "model_reasoning_effort=high"

    def test_codex_reasoning_effort_none(
        self, codex_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=codex_profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        # No -c config override for reasoning when not set
        assert "model_reasoning_effort=" not in " ".join(cmd)

    def test_codex_reasoning_effort_in_command_position(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            model="o4-mini",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
            reasoning_effort="medium",
        )
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        m_idx = cmd.index("-m")
        c_idx = cmd.index("-c")
        C_idx = cmd.index("-C")
        assert m_idx < c_idx < C_idx
        assert cmd[c_idx + 1] == "model_reasoning_effort=medium"

    def test_codex_is_pool_suppresses_session_id(
        self, codex_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """is_pool=True must return session_id=None even if the index file exists."""
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="reviewed", stderr=""
        )
        # Patch _get_codex_session_id to prove it is never called in pool mode.
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc):
            with patch(
                "theforge.runners.runner_codex._get_codex_session_id", return_value="some-uuid"
            ) as mock_extract:
                result = run_agent(
                    prompt="review",
                    profile=codex_profile,
                    working_dir=tmp_path,
                    is_pool=True,
                )

        mock_extract.assert_not_called()
        assert result.session_id is None

    def test_codex_sequential_extracts_session_id(
        self, codex_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """is_pool=False (default) calls _get_codex_session_id and returns its result."""
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="reviewed", stderr=""
        )
        with patch(_CODEX_RUN_TARGET, return_value=mock_proc):
            with patch(
                "theforge.runners.runner_codex._get_codex_session_id", return_value="abc-123"
            ) as mock_extract:
                result = run_agent(
                    prompt="review",
                    profile=codex_profile,
                    working_dir=tmp_path,
                    is_pool=False,
                )

        mock_extract.assert_called_once()
        assert result.session_id == "abc-123"

    def test_codex_rate_limit_falls_back_to_openai_api(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash"),
            api_fallback=TransportFallbackConfig(provider="openai", model="o4-mini"),
        )
        cli_result = AgentResult(
            success=False,
            output="429 rate limit exceeded",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="codex-reviewer",
        )
        api_result = AgentResult(
            success=True,
            output="api fallback result",
            session_id=None,
            cost_usd=0.12,
            exit_code=0,
            raw={},
            profile_name="codex-reviewer",
        )

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_result),
            patch("theforge.runners.api.run_api_agent", return_value=api_result) as mock_api,
        ):
            result = run_agent(prompt="review", profile=profile, working_dir=tmp_path)

        fallback_profile = mock_api.call_args.kwargs["profile"]
        assert fallback_profile.cli is None
        assert fallback_profile.provider == "openai"
        assert fallback_profile.model == "o4-mini"
        assert fallback_profile.allowed_tools == ("Read", "Bash")
        assert result == dataclasses.replace(
            api_result,
            model_used="o4-mini",
            cli_quota_error_observed=True,
            transport_fallback_fired=True,
            transport_fallback_reason="matched '429'",
            transport_used="api",
            # Configured stays what the operator selected even though the
            # invocation resolved onto the API fallback — that divergence is the
            # fact the ledger exists to keep (#2205).
            configured_model="gpt-5.4",
            configured_transport="cli",
        )

    def test_codex_non_retryable_failure_does_not_fall_back(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
            api_fallback=TransportFallbackConfig(provider="openai", model="o4-mini"),
        )
        cli_result = AgentResult(
            success=False,
            output="syntax error in prompt",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="codex-reviewer",
        )

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_result),
            patch("theforge.runners.api.run_api_agent") as mock_api,
        ):
            result = run_agent(prompt="review", profile=profile, working_dir=tmp_path)

        mock_api.assert_not_called()
        # model_used is set to profile.model even when no fallback fires
        assert result == dataclasses.replace(
            cli_result,
            model_used="gpt-5.4",
            transport_used="cli",
            configured_model="gpt-5.4",
            configured_transport="cli",
        )

    def test_codex_startup_failure_falls_back_to_openai_api(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
            api_fallback=TransportFallbackConfig(provider="openai", model="o4-mini"),
        )
        cli_result = AgentResult(
            success=False,
            output="ERROR: 'npx @openai/codex' CLI not found. Is it installed?",
            session_id=None,
            cost_usd=None,
            exit_code=-1,
            raw={},
            profile_name="codex-reviewer",
            startup_failure=True,
        )
        api_result = AgentResult(
            success=True,
            output="api fallback result",
            session_id=None,
            cost_usd=0.12,
            exit_code=0,
            raw={},
            profile_name="codex-reviewer",
        )

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_result),
            patch("theforge.runners.api.run_api_agent", return_value=api_result) as mock_api,
        ):
            result = run_agent(prompt="review", profile=profile, working_dir=tmp_path)

        mock_api.assert_called_once()
        assert result == dataclasses.replace(
            api_result,
            model_used="o4-mini",
            cli_quota_error_observed=False,
            transport_fallback_fired=True,
            transport_fallback_reason="CLI unavailable",
            transport_used="api",
            configured_model="gpt-5.4",
            configured_transport="cli",
        )

    def test_codex_resumed_session_falls_back_to_api_and_drops_cli_session(
        self, tmp_path: Path
    ) -> None:
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
            api_fallback=TransportFallbackConfig(provider="openai", model="o4-mini"),
        )
        cli_result = AgentResult(
            success=False,
            output="429 rate limit exceeded",
            session_id="cli-session",
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="codex-reviewer",
        )
        api_result = AgentResult(
            success=True,
            output="api fallback result",
            session_id=None,
            cost_usd=0.12,
            exit_code=0,
            raw={},
            profile_name="codex-reviewer",
        )

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_result),
            patch("theforge.runners.api.run_api_agent", return_value=api_result) as mock_api,
        ):
            result = run_agent(
                prompt="retry review",
                profile=profile,
                working_dir=tmp_path,
                session_id="cli-session",
            )

        mock_api.assert_called_once()
        assert result == dataclasses.replace(
            api_result,
            model_used="o4-mini",
            cli_quota_error_observed=True,
            transport_fallback_fired=True,
            transport_fallback_reason="matched '429'",
            transport_used="api",
            # Configured stays what the operator selected even though the
            # invocation resolved onto the API fallback — that divergence is the
            # fact the ledger exists to keep (#2205).
            configured_model="gpt-5.4",
            configured_transport="cli",
        )

    def test_codex_api_fallback_inherits_optional_profile_fields(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="codex-reviewer",
            cli="codex",
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash"),
            reasoning_effort="high",
            base_url="https://example.invalid/v1",
            max_iterations=7,
            api_fallback=TransportFallbackConfig(provider="openai", model="o4-mini"),
        )
        cli_result = AgentResult(
            success=False,
            output="429 rate limit exceeded",
            session_id=None,
            cost_usd=None,
            exit_code=1,
            raw={},
            profile_name="codex-reviewer",
        )
        api_result = AgentResult(
            success=True,
            output="api fallback result",
            session_id=None,
            cost_usd=0.12,
            exit_code=0,
            raw={},
            profile_name="codex-reviewer",
        )

        with (
            patch("theforge.runners.runner_codex._run_codex", return_value=cli_result),
            patch("theforge.runners.api.run_api_agent", return_value=api_result) as mock_api,
        ):
            result = run_agent(prompt="review", profile=profile, working_dir=tmp_path)

        fallback_profile = mock_api.call_args.kwargs["profile"]
        assert fallback_profile.reasoning_effort == "high"
        assert fallback_profile.base_url == "https://example.invalid/v1"
        assert fallback_profile.max_iterations == 7
        assert result == dataclasses.replace(
            api_result,
            model_used="o4-mini",
            cli_quota_error_observed=True,
            transport_fallback_fired=True,
            transport_fallback_reason="matched '429'",
            transport_used="api",
            # Configured stays what the operator selected even though the
            # invocation resolved onto the API fallback — that divergence is the
            # fact the ledger exists to keep (#2205).
            configured_model="gpt-5.4",
            configured_transport="cli",
            # Recorded from the profile that dispatched, so the ledger states the
            # conditions the invocation ran under, not just its identity.
            reasoning_effort="high",
        )


class TestRunGemini:
    """Test Gemini CLI subprocess invocation."""

    def test_gemini_success(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "Looks good to me."})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc):
            result = run_agent(
                prompt="review this code",
                profile=gemini_profile,
                working_dir=tmp_path,
            )

        assert result.success is True
        assert result.output == "Looks good to me."
        assert result.session_id == "latest"  # gemini always returns "latest" for resume
        assert result.cost_usd is None
        assert result.exit_code == 0
        assert result.profile_name == "gemini-reviewer"

    def test_gemini_timeout(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            _GEMINI_RUN_TARGET,
            side_effect=subprocess.TimeoutExpired(cmd="gemini", timeout=300),
        ):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.success is False
        assert "TIMEOUT" in result.output
        assert "300" in result.output
        assert result.exit_code == -1

    def test_gemini_not_found(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            _GEMINI_RUN_TARGET,
            side_effect=FileNotFoundError(),
        ):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.success is False
        assert "not found" in result.output
        assert result.exit_code == -1

    def test_gemini_command_structure(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        # The command may be wrapped by a platform sandbox (sandbox-exec / bwrap).
        # Verify the Gemini CLI flags are present anywhere in the command, not at a fixed index.
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=gemini_profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "npx" in cmd
        assert "@google/gemini-cli" in cmd
        assert "-p" in cmd
        assert "--yolo" in cmd
        assert "-m" in cmd
        assert "gemini-2.5-pro" in cmd
        assert "-o" in cmd
        assert "json" in cmd

    def test_gemini_prompt_passed_via_flag(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """Prompt is passed via -p flag, not via stdin input=.

        The command may be wrapped by a platform sandbox (sandbox-exec / bwrap),
        so we find -p by index rather than assuming a fixed position.
        """
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc) as mock_run:
            run_agent(prompt="my prompt", profile=gemini_profile, working_dir=tmp_path)

        call_kwargs = mock_run.call_args[1]
        assert "input" not in call_kwargs
        cmd = mock_run.call_args[0][0]
        # The command may be sandbox-wrapped (sandbox-exec also uses -p for its policy arg).
        # Find -p that appears after @google/gemini-cli to get the prompt flag.
        gemini_start = cmd.index("@google/gemini-cli")
        gemini_args = cmd[gemini_start:]
        p_idx = gemini_args.index("-p")
        assert gemini_args[p_idx + 1] == "my prompt"

    def test_gemini_non_json_output(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="plain text response", stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.output == "plain text response"
        assert result.cost_usd is None

    def test_gemini_empty_output_falls_back_to_stderr(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gemini error"
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.output == "gemini error"

    def test_gemini_cwd_set(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        """Gemini uses cwd= on subprocess.run."""
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc) as mock_run:
            run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert mock_run.call_args[1]["cwd"] == str(tmp_path)

    def test_gemini_is_pool_suppresses_session_id(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """is_pool=True must return session_id=None to avoid trampling parallel reviewers."""
        json_output = json.dumps({"result": "looks good"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc):
            result = run_agent(
                prompt="review",
                profile=gemini_profile,
                working_dir=tmp_path,
                is_pool=True,
            )

        assert result.session_id is None

    def test_gemini_sequential_returns_latest(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """is_pool=False (default) returns 'latest' so the next sequential call can resume."""
        json_output = json.dumps({"result": "looks good"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc):
            result = run_agent(
                prompt="review",
                profile=gemini_profile,
                working_dir=tmp_path,
                is_pool=False,
            )

        assert result.session_id == "latest"

    def test_gemini_parse_failure_returns_none_session_id(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """Parse failure always returns session_id=None regardless of is_pool.

        A failed invocation may not have created a resumable session; resuming
        it would attach the next call to stale or non-existent context.
        """
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json at all", stderr=""
        )
        with patch(_GEMINI_RUN_TARGET, return_value=mock_proc):
            # Test both sequential and pool modes
            result_seq = run_agent(
                prompt="review",
                profile=gemini_profile,
                working_dir=tmp_path,
                is_pool=False,
            )
            result_pool = run_agent(
                prompt="review",
                profile=gemini_profile,
                working_dir=tmp_path,
                is_pool=True,
            )

        assert result_seq.session_id is None
        assert result_pool.session_id is None


class TestSecretsInjection:
    """AC-3: secrets are merged into subprocess env; os.environ is never mutated."""

    def test_run_agent_merges_secrets_into_claude_env(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """Secrets passed to run_agent() reach the claude subprocess env."""
        captured_env: dict | None = None

        def fake_popen(cmd, **kwargs):
            nonlocal captured_env
            captured_env = kwargs.get("env")
            mock = _make_stream_mock(
                [_result_line(result="done", session_id="s1", total_cost_usd=0.0)]
            )
            mock.poll.return_value = 0
            return mock

        secrets = {"MY_SECRET_KEY": "secret-value"}
        with patch("subprocess.Popen", side_effect=fake_popen):
            run_agent(
                prompt="test",
                profile=dev_profile,
                working_dir=tmp_path,
                secrets=secrets,
            )

        assert captured_env is not None
        assert captured_env.get("MY_SECRET_KEY") == "secret-value"

    def test_run_agent_does_not_mutate_os_environ(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """run_agent() never mutates the parent os.environ dict."""
        secrets = {"INJECTED_KEY": "injected-value"}
        original_env = dict(os.environ)

        def fake_popen(cmd, **kwargs):
            return _make_stream_mock(
                [_result_line(result="done", session_id="s1", total_cost_usd=0.0)]
            )

        with patch("subprocess.Popen", side_effect=fake_popen):
            run_agent(
                prompt="test",
                profile=dev_profile,
                working_dir=tmp_path,
                secrets=secrets,
            )

        assert "INJECTED_KEY" not in os.environ
        assert dict(os.environ) == original_env

    def test_run_agent_no_secrets_does_not_break(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """run_agent() works correctly when secrets is not provided."""

        def fake_popen(cmd, **kwargs):
            return _make_stream_mock(
                [_result_line(result="done", session_id="s1", total_cost_usd=0.0)]
            )

        with patch("subprocess.Popen", side_effect=fake_popen):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success


class TestLogAgentResult:
    """Test log output formatting."""

    def test_success_log(self, capsys: pytest.CaptureFixture) -> None:
        result = AgentResult(
            success=True,
            output="x" * 100,
            session_id="s1",
            cost_usd=0.5,
            exit_code=0,
            raw={},
        )
        set_log_level(LogLevel.VERBOSE)
        try:
            log_agent_result(result, "dev")
        finally:
            set_log_level(LogLevel.PROGRESS)
        captured = capsys.readouterr()
        assert "OK" in captured.err
        assert "dev" in captured.err
        assert "$0.500" in captured.err

    def test_failure_log(self, capsys: pytest.CaptureFixture) -> None:
        result = AgentResult(
            success=False,
            output="err",
            session_id=None,
            cost_usd=0.0,
            exit_code=1,
            raw={},
        )
        set_log_level(LogLevel.VERBOSE)
        try:
            log_agent_result(result, "review")
        finally:
            set_log_level(LogLevel.PROGRESS)
        captured = capsys.readouterr()
        assert "FAIL" in captured.err
        assert "review" in captured.err
