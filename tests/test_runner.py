"""Tests for runner.py — agent subprocess invocation."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import theforge.runner as runner_mod
from theforge.config import ModelProfile
from theforge.runner import AgentResult, LogLevel, log_agent_result, run_agent, run_agent_pool


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


class TestHybridRunner:
    @pytest.fixture
    def api_profile(self) -> ModelProfile:
        return ModelProfile(
            name="api-reviewer",
            provider="openai",
            model="o4-mini",
            budget_usd=1.0,
            timeout_seconds=120,
            allowed_tools=(),
        )

    def test_run_agent_api_dispatch(self, api_profile: ModelProfile, tmp_path: Path) -> None:
        """run_agent dispatches to runner_api.run_api_agent for API profiles."""
        mock_result = AgentResult(
            success=True,
            output="api result",
            session_id=None,
            cost_usd=0.1,
            exit_code=0,
            raw={},
            profile_name="api-reviewer",
            structured_data={"verdict": "APPROVE"},
        )
        with patch("theforge.runner_api.run_api_agent", return_value=mock_result) as mock_api_run:
            result = run_agent(prompt="test", profile=api_profile, working_dir=tmp_path)

        mock_api_run.assert_called_once_with(prompt="test", profile=api_profile, quiet=False)
        assert result == mock_result

    def test_run_agent_cli_dispatch(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """run_agent dispatches to CLI runner for CLI profiles."""
        with patch("theforge.runner._run_claude") as mock_cli_run:
            run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
        mock_cli_run.assert_called_once()

    def test_run_agent_pool_mixed(
        self, dev_profile: ModelProfile, api_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """run_agent_pool handles a mix of CLI and API profiles."""
        cli_result = AgentResult(
            success=True, output="cli", cost_usd=None, exit_code=0, raw={}, session_id="s1"
        )
        api_result = AgentResult(
            success=True, output="api", cost_usd=0.1, exit_code=0, raw={}, session_id=None
        )

        def mock_run_agent_selector(**kwargs):
            if kwargs["profile"].mode == "api":
                return api_result
            return cli_result

        with patch("theforge.runner.run_agent", side_effect=mock_run_agent_selector) as mock_run:
            results = run_agent_pool(
                prompt="test", profiles=[dev_profile, api_profile], working_dir=tmp_path
            )

        assert len(results) == 2
        assert results[0] == cli_result
        assert results[1] == api_result
        assert mock_run.call_count == 2


class TestRunAgentClaude:
    """Test Claude CLI subprocess invocation."""

    def test_happy_path(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(
            [
                _result_line(
                    result="I implemented the feature.",
                    session_id="sess-abc123",
                    total_cost_usd=0.42,
                )
            ]
        )
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
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

        # Verify CLI args passed to Popen
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        fmt_idx = cmd.index("--output-format")
        assert cmd[fmt_idx + 1] == "stream-json"
        assert "--verbose" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd
        assert "--allowedTools" in cmd
        assert call_args[1]["cwd"] == str(tmp_path)

        # Prompt written to stdin
        mock_proc.stdin.write.assert_called_once_with("implement the thing")
        mock_proc.stdin.close.assert_called_once()

    def test_claudecode_env_stripped(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """CLAUDECODE must be absent from the subprocess env to bypass nested-session check."""
        import os

        mock_proc = _make_stream_mock([_result_line(result="done")])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
            with patch.dict(os.environ, {"CLAUDECODE": "1"}):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        env_passed = mock_popen.call_args[1]["env"]
        assert "CLAUDECODE" not in env_passed

    def test_with_session_resume(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(
            [_result_line(result="continued.", session_id="sess-abc123")]
        )
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
            run_agent(
                prompt="continue",
                profile=dev_profile,
                working_dir=tmp_path,
                session_id="sess-abc123",
            )

        cmd = mock_popen.call_args[0][0]
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
        mock_proc = _make_stream_mock([_result_line(result="done")])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc) as mock_popen:
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)

        cmd = mock_popen.call_args[0][0]
        assert "--allowedTools" not in cmd

    def test_nonzero_exit(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(
            [_result_line(result="partial work", total_cost_usd=0.15)],
            returncode=1,
        )
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.exit_code == 1
        assert result.output == "partial work"
        assert result.cost_usd == 0.15

    def test_non_json_output(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(["plain text output"])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.output == "plain text output"
        assert result.session_id is None
        assert result.cost_usd == None

    def test_empty_output(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock([], returncode=1, stderr="some error")
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.output == "some error"

    def test_timeout(self, tmp_path: Path) -> None:
        # Use a 1-second timeout so the test completes quickly.
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=1,
            allowed_tools=(),
        )

        class _BlockingStdout:
            """Simulates a stdout that blocks longer than the timeout."""

            def __iter__(self):
                time.sleep(5)  # longer than timeout_seconds=1
                return iter([])

        mock_proc = MagicMock()
        mock_proc.stdout = _BlockingStdout()
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.returncode = -1
        mock_proc.wait.return_value = -1
        mock_proc.poll.return_value = None

        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert "TIMEOUT" in result.output
        assert result.exit_code == -9

    def test_timeout_returns_session_id(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=1,
            allowed_tools=(),
        )

        class _PartialStdout:
            def __iter__(self):
                yield json.dumps({"type": "assistant", "session_id": "sess-timeout"}) + "\n"
                time.sleep(2)
                return

        mock_proc = MagicMock()
        mock_proc.stdout = _PartialStdout()
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.returncode = -1
        mock_proc.wait.return_value = -1
        mock_proc.poll.return_value = None

        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert result.exit_code == -9
        assert result.session_id == "sess-timeout"

    def test_cli_not_found(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runner.subprocess.Popen",
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
        mock_proc = _make_stream_mock([_result_line(result="reviewed.", total_cost_usd=0.10)])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="review this", profile=review_profile, working_dir=tmp_path)

        assert result.profile_name == "review"

    def test_activity_printed(
        self, dev_profile: ModelProfile, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """tool_use_summary events must be printed to stderr in verbose mode."""
        summary_line = (
            json.dumps(
                {
                    "type": "tool_use_summary",
                    "summary": "Read src/theforge/runner.py (240 lines)",
                }
            )
            + "\n"
        )
        mock_proc = _make_stream_mock(
            [summary_line, _result_line(result="done", total_cost_usd=0.01)]
        )
        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)

        captured = capsys.readouterr()
        assert "↳ Read src/theforge/runner.py (240 lines)" in captured.err

    def test_activity_label_only_in_pool_mode(
        self, dev_profile: ModelProfile, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Label prefix [name] appears in parallel pool mode (quiet=True) only."""
        summary_line = (
            json.dumps(
                {
                    "type": "tool_use_summary",
                    "summary": "Read file.py (10 lines)",
                }
            )
            + "\n"
        )
        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            # Single-agent mode (quiet=False, default): no label prefix
            mock_proc = _make_stream_mock(
                [summary_line, _result_line(result="done", total_cost_usd=0.01)]
            )
            with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
            captured = capsys.readouterr()
            assert "↳ Read file.py (10 lines)" in captured.err
            assert "[dev]" not in captured.err

            # Pool mode (quiet=True): label prefix present
            mock_proc2 = _make_stream_mock(
                [summary_line, _result_line(result="done", total_cost_usd=0.01)]
            )
            with patch("theforge.runner.subprocess.Popen", return_value=mock_proc2):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path, quiet=True)
            captured2 = capsys.readouterr()
            assert "↳ [dev] Read file.py (10 lines)" in captured2.err
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)

    def test_activity_assistant_fallback(
        self, dev_profile: ModelProfile, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """assistant tool_use events fall back to name: input_preview format."""
        assistant_line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "pytest tests/ -q"},
                            }
                        ]
                    },
                }
            )
            + "\n"
        )
        mock_proc = _make_stream_mock(
            [assistant_line, _result_line(result="done", total_cost_usd=0.01)]
        )
        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)

        captured = capsys.readouterr()
        assert "↳ Bash: pytest tests/ -q" in captured.err

    def test_no_result_json_fallback_extracts_session_id(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = _make_stream_mock(
            [json.dumps({"type": "assistant", "session_id": "sess-fallback"}) + "\n"]
        )
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.session_id == "sess-fallback"
        assert result.output == json.dumps({"type": "assistant", "session_id": "sess-fallback"})


class TestClaudeSessionIdHelper:
    def test_get_claude_session_id_from_jsonl(self, tmp_path: Path) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "assistant", "session_id": "sess-from-jsonl"}),
                json.dumps({"type": "result", "result": "done"}),
            ]
        )

        sid = runner_mod._get_claude_session_id(output, tmp_path)

        assert sid == "sess-from-jsonl"

    def test_get_claude_session_id_fallback_to_file(self, tmp_path: Path) -> None:
        project_slug = str(tmp_path.resolve()).replace("/", "-")
        claude_dir = tmp_path / ".claude" / "projects" / project_slug
        claude_dir.mkdir(parents=True)
        old_file = claude_dir / "sess-old.jsonl"
        new_file = claude_dir / "sess-new.jsonl"
        old_file.write_text("", encoding="utf-8")
        new_file.write_text("", encoding="utf-8")
        now = time.time()
        os.utime(old_file, (now - 20, now - 20))
        os.utime(new_file, (now - 5, now - 5))

        with patch("theforge.runner.Path.home", return_value=tmp_path):
            sid = runner_mod._get_claude_session_id("", tmp_path, min_mtime=now - 10)

        assert sid == "sess-new"

    def test_get_claude_session_id_no_fallback_for_pool(self, tmp_path: Path) -> None:
        project_slug = str(tmp_path.resolve()).replace("/", "-")
        claude_dir = tmp_path / ".claude" / "projects" / project_slug
        claude_dir.mkdir(parents=True)
        (claude_dir / "sess-from-disk.jsonl").write_text("", encoding="utf-8")

        with patch("theforge.runner.Path.home", return_value=tmp_path):
            sid = runner_mod._get_claude_session_id(
                "",
                tmp_path,
                fallback_to_file=False,
                min_mtime=time.time() - 10,
            )

        assert sid is None


class TestRunAgentCostCoercion:
    """Test that non-numeric cost_usd is coerced to float safely."""

    def test_string_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd="0.42")])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd == 0.42
        assert isinstance(result.cost_usd, float)

    def test_null_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock([_result_line(result="done", cost_usd=None)])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd is None

    def test_garbage_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd="not-a-number")])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd is None


class TestRunAgentModelUsage:
    """Test per-model usage breakdown parsing from Claude JSON output."""

    def test_model_usage_parsed(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(
            [
                _result_line(
                    result="done",
                    total_cost_usd=0.123,
                    modelUsage={
                        "claude-sonnet-4-6": {
                            "inputTokens": 1000,
                            "outputTokens": 500,
                            "cacheReadInputTokens": 8000,
                            "cacheCreationInputTokens": 2000,
                            "costUSD": 0.123,
                        }
                    },
                )
            ]
        )
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
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
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.05)])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="do it", profile=dev_profile, working_dir=tmp_path)

        assert result.model_usage == ()

    def test_multi_model_usage(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """modelUsage can contain multiple models (e.g. tool use with different models)."""
        mock_proc = _make_stream_mock(
            [
                _result_line(
                    result="done",
                    total_cost_usd=0.20,
                    modelUsage={
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
                )
            ]
        )
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
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

    def test_pool_preserves_profile_order(self, tmp_path: Path) -> None:
        """run_agent_pool returns results in profile order regardless of completion order."""
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

        def mock_run_agent(**kwargs):
            profile = kwargs["profile"]
            output = "Review A output" if profile.name == "reviewer-a" else "Review B output"
            return AgentResult(
                success=True,
                output=output,
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
        mock_proc = _make_stream_mock([_result_line(result="solo review", total_cost_usd=0.20)])
        with patch("theforge.runner.subprocess.Popen", return_value=mock_proc):
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
        # Set comparison: parallel dispatch order is non-deterministic
        assert set(dispatched_clis) == {"claude", "gemini"}
        assert results[0].profile_name == "claude-reviewer"
        assert results[1].profile_name == "gemini-reviewer"

    def test_pool_passes_session_ids(self, tmp_path: Path) -> None:
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
        seen_session_ids: list[str | None] = []

        def mock_run_agent(**kwargs):
            seen_session_ids.append(kwargs.get("session_id"))
            profile = kwargs["profile"]
            return AgentResult(
                success=True,
                output=f"output-{profile.name}",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=mock_run_agent):
            run_agent_pool(
                prompt="review",
                profiles=profiles,
                working_dir=tmp_path,
                session_ids=["sess-1", "sess-2"],
            )

        assert seen_session_ids == ["sess-1", "sess-2"]

    def test_pool_session_ids_length_mismatch(self, tmp_path: Path) -> None:
        profiles = [
            ModelProfile(
                name="solo",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            )
        ]

        with pytest.raises(AssertionError, match="session_ids must match profiles length"):
            run_agent_pool(
                prompt="review",
                profiles=profiles,
                working_dir=tmp_path,
                session_ids=["a", "b"],
            )

    def test_pool_single_agent_passes_session_id(self, tmp_path: Path) -> None:
        profile = ModelProfile(
            name="solo",
            cli="claude",
            model="opus",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        seen_kwargs: dict[str, object] = {}

        def mock_run_agent(**kwargs):
            seen_kwargs.update(kwargs)
            return AgentResult(
                success=True,
                output="solo review",
                session_id="sess-solo",
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name="solo",
            )

        with patch("theforge.runner.run_agent", side_effect=mock_run_agent):
            results = run_agent_pool(
                prompt="review",
                profiles=[profile],
                working_dir=tmp_path,
                session_ids=["sess-prev"],
            )

        assert results[0].session_id == "sess-solo"
        assert seen_kwargs["session_id"] == "sess-prev"
        assert results[0].output == "solo review"

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

        def mock_run_agent(**kwargs):
            profile = kwargs["profile"]
            return AgentResult(
                success=True,
                output="done",
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

        assert results[0].profile_name == "r1"
        assert results[1].profile_name == "r2"

    def test_pool_runs_parallel(self, tmp_path: Path) -> None:
        """Wall clock is less than the sum of individual durations (proves parallel)."""
        profiles = [
            ModelProfile(
                name="a",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="b",
                cli="claude",
                model="sonnet",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="c",
                cli="claude",
                model="haiku",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
        ]

        def slow_agent(**kwargs) -> AgentResult:
            time.sleep(0.1)
            profile = kwargs["profile"]
            return AgentResult(
                success=True,
                output="done",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=slow_agent):
            start = time.monotonic()
            results = run_agent_pool(prompt="review", profiles=profiles, working_dir=tmp_path)
            elapsed = time.monotonic() - start

        assert len(results) == 3
        # Sequential total would be ~0.3s; parallel should finish in ~0.1s
        assert elapsed < 0.25

    def test_pool_preserves_order(self, tmp_path: Path) -> None:
        """Results are returned in profile order even when fast agent finishes first."""
        profiles = [
            ModelProfile(
                name="slow",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="fast",
                cli="claude",
                model="haiku",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
        ]

        def variable_agent(**kwargs) -> AgentResult:
            profile = kwargs["profile"]
            if profile.name == "slow":
                time.sleep(0.1)
            return AgentResult(
                success=True,
                output=f"output-{profile.name}",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=variable_agent):
            results = run_agent_pool(prompt="review", profiles=profiles, working_dir=tmp_path)

        assert results[0].profile_name == "slow"
        assert results[1].profile_name == "fast"
        assert results[0].output == "output-slow"
        assert results[1].output == "output-fast"

    def test_pool_single_agent_no_thread(self, tmp_path: Path) -> None:
        """Single-profile pool runs directly without creating a ThreadPoolExecutor."""
        profile = ModelProfile(
            name="solo",
            cli="claude",
            model="opus",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )

        def mock_run_agent(**kwargs) -> AgentResult:
            return AgentResult(
                success=True,
                output="solo",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name="solo",
            )

        with (
            patch("theforge.runner.run_agent", side_effect=mock_run_agent),
            patch("theforge.runner.ThreadPoolExecutor") as mock_executor,
        ):
            results = run_agent_pool(prompt="review", profiles=[profile], working_dir=tmp_path)

        mock_executor.assert_not_called()
        assert len(results) == 1
        assert results[0].profile_name == "solo"

    def test_pool_agent_failure_isolated(self, tmp_path: Path) -> None:
        """A failing agent doesn't prevent the others from returning results."""
        profiles = [
            ModelProfile(
                name="good",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="bad",
                cli="claude",
                model="sonnet",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
            ModelProfile(
                name="also-good",
                cli="claude",
                model="haiku",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
            ),
        ]

        def flaky_agent(**kwargs) -> AgentResult:
            profile = kwargs["profile"]
            if profile.name == "bad":
                raise RuntimeError("agent crashed")
            return AgentResult(
                success=True,
                output=f"output-{profile.name}",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=flaky_agent):
            results = run_agent_pool(prompt="review", profiles=profiles, working_dir=tmp_path)

        assert len(results) == 3
        assert results[0].success is True
        assert results[0].output == "output-good"
        assert results[1].success is False
        assert results[1].exit_code == -1
        assert results[2].success is True
        assert results[2].output == "output-also-good"

    def test_pool_progress_logging(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Pool emits start banner, per-agent completion, and final summary to stderr."""
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

        def mock_run_agent(**kwargs) -> AgentResult:
            profile = kwargs["profile"]
            return AgentResult(
                success=True,
                output="done",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=mock_run_agent):
            run_agent_pool(prompt="review", profiles=profiles, working_dir=tmp_path)

        captured = capsys.readouterr()
        # R3: pool start banner lists agent names
        assert "Starting review pool:" in captured.err
        assert "reviewer-a" in captured.err
        assert "reviewer-b" in captured.err
        assert "(parallel)" in captured.err
        # R3: per-agent completion lines with duration
        assert "reviewer-a" in captured.err
        assert "reviewer-b" in captured.err
        assert "done" in captured.err
        # R3: final summary with wall clock and sequential estimate
        assert "Review pool complete:" in captured.err
        assert "wall clock" in captured.err
        assert "sequential" in captured.err

    def test_pool_all_agents_receive_same_prompt(self, tmp_path: Path) -> None:
        """All agents in the pool receive exactly the same prompt string."""
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
        import threading

        received_prompts: list[str] = []
        lock = threading.Lock()

        def capture_prompt(**kwargs) -> AgentResult:
            with lock:
                received_prompts.append(kwargs["prompt"])
            profile = kwargs["profile"]
            return AgentResult(
                success=True,
                output="done",
                session_id=None,
                cost_usd=0.0,
                exit_code=0,
                raw={},
                profile_name=profile.name,
            )

        with patch("theforge.runner.run_agent", side_effect=capture_prompt):
            run_agent_pool(prompt="the shared prompt", profiles=profiles, working_dir=tmp_path)

        assert len(received_prompts) == 2
        assert all(p == "the shared prompt" for p in received_prompts)


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
        assert result.cost_usd is None
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
        assert result.cost_usd is None

    def test_codex_empty_output_falls_back_to_stderr(
        self, codex_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="codex error"
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
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
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
            run_agent(prompt="review", profile=profile, working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "-c" in cmd
        c_idx = cmd.index("-c")
        assert cmd[c_idx + 1] == "model_reasoning_effort=high"

    def test_codex_reasoning_effort_none(
        self, codex_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
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
        with patch("theforge.runner.subprocess.run", return_value=mock_proc) as mock_run:
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
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            with patch(
                "theforge.runner._get_codex_session_id", return_value="some-uuid"
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
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
            with patch(
                "theforge.runner._get_codex_session_id", return_value="abc-123"
            ) as mock_extract:
                result = run_agent(
                    prompt="review",
                    profile=codex_profile,
                    working_dir=tmp_path,
                    is_pool=False,
                )

        mock_extract.assert_called_once()
        assert result.session_id == "abc-123"


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
        assert result.session_id == "latest"  # gemini always returns "latest" for resume
        assert result.cost_usd is None
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
        assert cmd[0] == "npx"
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
        assert result.cost_usd is None

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

    def test_gemini_is_pool_suppresses_session_id(
        self, gemini_profile: ModelProfile, tmp_path: Path
    ) -> None:
        """is_pool=True must return session_id=None to avoid trampling parallel reviewers."""
        json_output = json.dumps({"result": "looks good"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json_output, stderr=""
        )
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
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
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
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
        with patch("theforge.runner.subprocess.run", return_value=mock_proc):
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
        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            log_agent_result(result, "dev")
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)
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
        runner_mod.set_log_level(LogLevel.VERBOSE)
        try:
            log_agent_result(result, "review")
        finally:
            runner_mod.set_log_level(LogLevel.PROGRESS)
        captured = capsys.readouterr()
        assert "FAIL" in captured.err
        assert "review" in captured.err
