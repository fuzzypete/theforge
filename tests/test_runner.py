"""Tests for runner.py — agent subprocess invocation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config import ModelProfile
from theforge.runner import AgentResult, log_agent_result, run_agent, run_agent_pool


@pytest.fixture
def dev_profile() -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=2.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash"),
    )


@pytest.fixture
def review_profile() -> ModelProfile:
    return ModelProfile(
        name="review",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


class TestRunAgentClaude:
    """Test Claude CLI subprocess invocation."""

    def test_happy_path(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps(
            {
                "result": "I implemented the feature.",
                "session_id": "sess-abc123",
                "total_cost_usd": 0.42,
            }
        )
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            result = run_agent(
                prompt="implement the thing",
                profile=dev_profile,
                working_dir=tmp_path,
            )

        assert result.success is True
        assert result.output == "I implemented the feature."
        assert result.session_id == "sess-abc123"
        assert result.cost_usd == 0.42
        assert result.exit_code == 0
        assert result.profile_name == "dev"

        # Verify CLI args
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd
        assert "--allowedTools" in cmd
        assert call_args[1]["input"] == "implement the thing"
        assert call_args[1]["cwd"] == str(tmp_path)

    def test_claudecode_env_stripped(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """CLAUDECODE must be absent from the subprocess env to bypass nested-session check."""
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        import os

        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            with patch.dict(os.environ, {"CLAUDECODE": "1"}):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        env_passed = mock_run.call_args[1]["env"]
        assert "CLAUDECODE" not in env_passed

    def test_with_session_resume(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "continued.", "session_id": "sess-abc123"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_agent(
                prompt="continue",
                profile=dev_profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )

        cmd = mock_run.call_args[0][0]
        assert "--resume" in cmd
        assert "sess-abc123" in cmd

    def test_no_allowed_tools(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="minimal",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--allowedTools" not in cmd

    def test_nonzero_exit(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "partial work", "total_cost_usd": 0.15})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.exit_code == 1
        assert result.output == "partial work"
        assert result.cost_usd == 0.15

    def test_non_json_output(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="plain text output", stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.output == "plain text output"
        assert result.session_id is None
        assert result.cost_usd == 0.0

    def test_empty_output(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="some error"
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.output == "some error"

    def test_timeout(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=900),
        ):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert "TIMEOUT" in result.output
        assert "900" in result.output
        assert result.exit_code == -1

    def test_cli_not_found(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runner.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert "not found" in result.output
        assert result.exit_code == -1

    def test_profile_name_set_on_result(
        self, review_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """AgentResult.profile_name must be set to profile.name."""
        json_output = json.dumps({"result": "reviewed.", "total_cost_usd": 0.10})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="review this", profile=review_profile, working_dir=tmp_path)

        assert result.profile_name == "review"


class TestRunAgentCostCoercion:
    """Test that non-numeric cost_usd is coerced to float safely."""

    def test_string_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "done", "total_cost_usd": "0.42"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd == 0.42
        assert isinstance(result.cost_usd, float)

    def test_null_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "done", "cost_usd": None})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd == 0.0

    def test_garbage_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "done", "total_cost_usd": "not-a-number"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd == 0.0


class TestRunAgentModelUsage:
    """Test per-model usage breakdown parsing from Claude JSON output."""

    def test_model_usage_parsed(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps(
            {
                "result": "done",
                "total_cost_usd": 0.123,
                "modelUsage": {
                    "claude-sonnet-4-6": {
                        "inputTokens": 1000,
                        "outputTokens": 500,
                        "cacheReadInputTokens": 8000,
                        "cacheCreationInputTokens": 2000,
                        "costUSD": 0.123,
                    }
                },
            }
        )
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="do it", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd == 0.123
        assert len(result.model_usage) == 1
        u = result.model_usage[0]
        assert u.model == "claude-sonnet-4-6"
        assert u.input_tokens == 1000
        assert u.output_tokens == 500
        assert u.cache_read_tokens == 8000
        assert u.cache_creation_tokens == 2000
        assert u.cost_usd == 0.123

    def test_model_usage_empty_when_absent(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        json_output = json.dumps({"result": "done", "total_cost_usd": 0.05})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="do it", profile=dev_profile, working_dir=tmp_path)

        assert result.model_usage == ()

    def test_multi_model_usage(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """modelUsage can contain multiple models (e.g. tool use with different models)."""
        json_output = json.dumps(
            {
                "result": "done",
                "total_cost_usd": 0.20,
                "modelUsage": {
                    "claude-sonnet-4-6": {
                        "inputTokens": 500,
                        "outputTokens": 200,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "costUSD": 0.10,
                    },
                    "claude-opus-4-6": {
                        "inputTokens": 300,
                        "outputTokens": 100,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "costUSD": 0.10,
                    },
                },
            }
        )
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="do it", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd == 0.20
        assert len(result.model_usage) == 2
        models = {u.model for u in result.model_usage}
        assert "claude-sonnet-4-6" in models
        assert "claude-opus-4-6" in models


class TestRunAgentUnknownCli:
    """Test dispatch for unsupported CLI."""

    def test_unknown_cli(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="dev",
            cli="llama",
            model="llama3",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        result = run_agent(prompt="test", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert "Unknown CLI" in result.output
        assert "llama" in result.output
        assert result.exit_code == -1
        assert result.profile_name == "dev"


class TestRunAgentPool:
    """Test pool runner."""

    def test_pool_runs_sequentially_in_order(self, tmp_path: Path) -> None:
        """run_agent_pool returns results in profile order."""
        profiles = [
            ModelProfile(
                name="reviewer-a",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="reviewer-b",
                cli="claude",
                model="sonnet",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
        ]
        outputs = ["Review A output", "Review B output"]
        call_index = {"n": 0}

        def mock_run_agent(**kwargs):
            idx = call_index["n"]
            call_index["n"] += 1
            profile = kwargs["profile"]
            return AgentResult(
                success=True,
                output=outputs[idx],
                session_id=None,
                cost_usd=0.10,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=mock_run_agent):
            results = run_agent_pool(
                prompt="review this",
                profiles=profiles,
                working_dir=tmp_path,
            )

        assert len(results) == 2
        assert results[0].output == "Review A output"
        assert results[0].profile_name == "reviewer-a"
        assert results[1].output == "Review B output"
        assert results[1].profile_name == "reviewer-b"

    def test_pool_of_one(self, tmp_path: Path) -> None:
        """Pool with 1 profile returns a list of 1 result."""
        profile = ModelProfile(
            name="solo",
            cli="claude",
            model="opus",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        json_output = json.dumps({"result": "solo review", "total_cost_usd": 0.20})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            results = run_agent_pool(
                prompt="review this",
                profiles=[profile],
                working_dir=tmp_path,
            )

        assert len(results) == 1
        assert results[0].output == "solo review"
        assert results[0].profile_name == "solo"

    def test_pool_mixed_clis(self, tmp_path: Path) -> None:
        """Pool with Claude and Gemini profiles dispatches correctly to each CLI."""
        profiles = [
            ModelProfile(
                name="claude-reviewer",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="gemini-reviewer",
                cli="gemini",
                model="gemini-2.5-pro",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
        ]
        dispatched_clis: list[str] = []

        def mock_run_agent(**kwargs):
            profile = kwargs["profile"]
            dispatched_clis.append(profile.cli)
            return AgentResult(
                success=True,
                output=f"output from {profile.cli}",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=mock_run_agent):
            results = run_agent_pool(
                prompt="review this",
                profiles=profiles,
                working_dir=tmp_path,
            )

        assert len(results) == 2
        assert dispatched_clis == ["claude", "gemini"]
        assert results[0].profile_name == "claude-reviewer"
        assert results[1].profile_name == "gemini-reviewer"
        assert results[0].output == "output from claude"
        assert results[1].output == "output from gemini"

    def test_pool_profile_name_set_on_results(self, tmp_path: Path) -> None:
        """Each result in the pool has profile_name matching the profile."""
        profiles = [
            ModelProfile(
                name="r1",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="r2",
                cli="claude",
                model="sonnet",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
        ]
        json_output = json.dumps({"result": "done", "total_cost_usd": 0.10})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            results = run_agent_pool(
                prompt="review this",
                profiles=profiles,
                working_dir=tmp_path,
            )

        assert results[0].profile_name == "r1"
        assert results[1].profile_name == "r2"


@pytest.fixture
def codex_profile() -> ModelProfile:
    return ModelProfile(
        name="codex-reviewer",
        cli="codex",
        model="o4-mini",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )


@pytest.fixture
def gemini_profile() -> ModelProfile:
    return ModelProfile(
        name="gemini-reviewer",
        cli="gemini",
        model="gemini-2.5-pro",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )


class TestRunCodex:
    """Test Codex CLI subprocess invocation."""

    def test_codex_success(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Code looks good.", stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(
                prompt="review this code",
                profile=codex_profile,
                working_dir=tmp_path,
            )

        assert result.success is True
        assert result.output == "Code looks good."
        assert result.session_id is None
        assert result.cost_usd == 0.0
        assert result.exit_code == 0
        assert result.profile_name == "codex-reviewer"

    def test_codex_timeout(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="npx", timeout=300),
        ):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.success is False
        assert "TIMEOUT" in result.output
        assert "300" in result.output
        assert result.exit_code == -1

    def test_codex_not_found(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runner.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.success is False
        assert "not found" in result.output
        assert result.exit_code == -1

    def test_codex_command_structure(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=codex_profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "npx" in cmd
        assert "@openai/codex" in cmd
        assert "exec" in cmd
        assert "--full-auto" in cmd
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
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.output == "fallback stdout"

    def test_codex_nonzero_exit(self, codex_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="partial work", stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.exit_code == 1
        assert result.output == "partial work"
        assert result.cost_usd == 0.0

    def test_codex_empty_output_falls_back_to_stderr(
        self, codex_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="codex error"
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=codex_profile, working_dir=tmp_path)

        assert result.output == "codex error"


class TestRunGemini:
    """Test Gemini CLI subprocess invocation."""

    def test_gemini_success(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "Looks good to me."})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(
                prompt="review this code",
                profile=gemini_profile,
                working_dir=tmp_path,
            )

        assert result.success is True
        assert result.output == "Looks good to me."
        assert result.session_id is None
        assert result.cost_usd == 0.0
        assert result.exit_code == 0
        assert result.profile_name == "gemini-reviewer"

    def test_gemini_timeout(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gemini", timeout=300),
        ):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.success is False
        assert "TIMEOUT" in result.output
        assert "300" in result.output
        assert result.exit_code == -1

    def test_gemini_not_found(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runner.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.success is False
        assert "not found" in result.output
        assert result.exit_code == -1

    def test_gemini_command_structure(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=gemini_profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gemini"
        assert "-p" in cmd
        assert "--yolo" in cmd
        assert "-m" in cmd
        assert "gemini-2.5-pro" in cmd
        assert "-o" in cmd
        assert "json" in cmd

    def test_gemini_prompt_passed_via_flag(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """Prompt is passed via -p flag, not via stdin input=."""
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_agent(prompt="my prompt", profile=gemini_profile, working_dir=tmp_path)

        call_kwargs = mock_run.call_args[1]
        assert "input" not in call_kwargs
        cmd = mock_run.call_args[0][0]
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "my prompt"

    def test_gemini_non_json_output(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="plain text response", stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.output == "plain text response"
        assert result.cost_usd == 0.0

    def test_gemini_empty_output_falls_back_to_stderr(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="gemini error"
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            result = run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert result.output == "gemini error"

    def test_gemini_cwd_set(self, gemini_profile: ModelProfile, tmp_path: Path) -> None:
        """Gemini uses cwd= on subprocess.run."""
        json_output = json.dumps({"result": "done"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_agent(prompt="test", profile=gemini_profile, working_dir=tmp_path)

        assert mock_run.call_args[1]["cwd"] == str(tmp_path)


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
        log_agent_result(result, "dev")
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
        log_agent_result(result, "review")
        captured = capsys.readouterr()
        assert "FAIL" in captured.err
        assert "review" in captured.err
