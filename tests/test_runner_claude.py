"""Tests for Claude CLI runner: hybrid dispatch, session helpers, cost coercion, model usage."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import theforge.runners.runner_claude as runner_claude_mod
from theforge.agent_types import AgentResult
from theforge.config import ModelProfile
from theforge.log_level import LogLevel, set_log_level
from theforge.runners import run_agent, run_agent_pool


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
        with patch("theforge.runners.api.run_api_agent", return_value=mock_result) as mock_api_run:
            result = run_agent(prompt="test", profile=api_profile, working_dir=tmp_path)

        mock_api_run.assert_called_once_with(
            prompt="test",
            profile=api_profile,
            working_dir=tmp_path,
            quiet=False,
            secrets={},
            plain_text=False,
        )
        assert result == mock_result

    def test_run_agent_api_dev_dispatch(self, tmp_path: Path) -> None:
        """run_agent dispatches to run_api_agent for a dev profile with provider set."""
        dev_api_profile = ModelProfile(
            name="dev",
            provider="openai",
            model="gpt-4o",
            budget_usd=2.0,
            timeout_seconds=1800,
            allowed_tools=("write_file", "edit_file", "read_file", "bash", "glob", "grep"),
        )
        mock_result = AgentResult(
            success=True,
            output="implementation done",
            session_id=None,
            cost_usd=0.5,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        with patch("theforge.runners.api.run_api_agent", return_value=mock_result) as mock_api:
            result = run_agent(
                prompt="implement it", profile=dev_api_profile, working_dir=tmp_path
            )

        mock_api.assert_called_once_with(
            prompt="implement it",
            profile=dev_api_profile,
            working_dir=tmp_path,
            quiet=False,
            secrets={},
            plain_text=False,
        )
        assert result == mock_result

    def test_run_agent_cli_dispatch(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """run_agent dispatches to CLI runner for CLI profiles."""
        with patch("theforge.runners.runner_claude._run_claude") as mock_cli_run:
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

        with patch(
            "theforge.runners.cli.run_agent", side_effect=mock_run_agent_selector
        ) as mock_run:
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
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
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
        claude_cmd = cmd
        if cmd[0] in {"sandbox-exec", "bwrap"}:
            claude_cmd = [
                arg for arg in cmd if arg == "claude" or arg.startswith("-") or arg == "sonnet"
            ]
            claude_idx = cmd.index("claude")
            claude_cmd = cmd[claude_idx:]
        assert claude_cmd[0] == "claude"
        assert "-p" in claude_cmd
        assert "--output-format" in claude_cmd
        fmt_idx = claude_cmd.index("--output-format")
        assert claude_cmd[fmt_idx + 1] == "stream-json"
        assert "--verbose" in claude_cmd
        assert "--model" in claude_cmd
        assert "sonnet" in claude_cmd
        assert "--allowedTools" in claude_cmd
        assert call_args[1]["cwd"] == str(tmp_path)

        # Prompt written to stdin
        mock_proc.stdin.write.assert_called_once_with("implement the thing")
        mock_proc.stdin.close.assert_called_once()

    def test_claudecode_env_stripped(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """CLAUDECODE must be absent from the subprocess env to bypass nested-session check."""
        import os

        mock_proc = _make_stream_mock([_result_line(result="done")])
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            with patch.dict(os.environ, {"CLAUDECODE": "1"}):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        env_passed = mock_popen.call_args[1]["env"]
        assert "CLAUDECODE" not in env_passed

    def test_with_session_resume(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(
            [_result_line(result="continued.", session_id="sess-abc123")]
        )
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
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
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)

        cmd = mock_popen.call_args[0][0]
        assert "--allowedTools" not in cmd

    def test_nonzero_exit(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(
            [_result_line(result="partial work", total_cost_usd=0.15)],
            returncode=1,
        )
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.exit_code == 1
        assert result.output == "partial work"
        assert result.cost_usd == 0.15

    def test_non_json_output(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock(["plain text output"])
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.output == "plain text output"
        assert result.session_id is None
        assert result.cost_usd is None

    def test_empty_output(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock([], returncode=1, stderr="some error")
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.output == "some error"

    def test_timeout(self, tmp_path: Path) -> None:
        # Use a short timeout so the test completes very quickly.
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=0.1,
            allowed_tools=(),
        )

        class _BlockingStdout:
            """Simulates a stdout that blocks longer than the timeout."""

            def __iter__(self):
                time.sleep(0.5)  # longer than timeout_seconds=0.1
                return iter([])

        mock_proc = MagicMock()
        mock_proc.stdout = _BlockingStdout()
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.returncode = -1
        mock_proc.wait.return_value = -1
        mock_proc.poll.return_value = None

        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
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
            timeout_seconds=0.1,
            allowed_tools=(),
        )

        class _PartialStdout:
            def __iter__(self):
                yield json.dumps({"type": "assistant", "session_id": "sess-timeout"}) + "\n"
                time.sleep(0.5)
                return

        mock_proc = MagicMock()
        mock_proc.stdout = _PartialStdout()
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.returncode = -1
        mock_proc.wait.return_value = -1
        mock_proc.poll.return_value = None

        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert result.exit_code == -9
        assert result.session_id == "sess-timeout"

    def test_cli_not_found(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen",
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
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
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
        set_log_level(LogLevel.VERBOSE)
        try:
            with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
        finally:
            set_log_level(LogLevel.PROGRESS)

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
        set_log_level(LogLevel.VERBOSE)
        try:
            # Single-agent mode (quiet=False, default): no label prefix
            mock_proc = _make_stream_mock(
                [summary_line, _result_line(result="done", total_cost_usd=0.01)]
            )
            with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
            captured = capsys.readouterr()
            assert "↳ Read file.py (10 lines)" in captured.err
            assert "[dev]" not in captured.err

            # Pool mode (quiet=True): label prefix present
            mock_proc2 = _make_stream_mock(
                [summary_line, _result_line(result="done", total_cost_usd=0.01)]
            )
            with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc2):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path, quiet=True)
            captured2 = capsys.readouterr()
            assert "↳ [dev] Read file.py (10 lines)" in captured2.err
        finally:
            set_log_level(LogLevel.PROGRESS)

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
        set_log_level(LogLevel.VERBOSE)
        try:
            with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
                run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
        finally:
            set_log_level(LogLevel.PROGRESS)

        captured = capsys.readouterr()
        assert "↳ Bash: pytest tests/ -q" in captured.err

    def test_no_result_json_fallback_extracts_session_id(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = _make_stream_mock(
            [json.dumps({"type": "assistant", "session_id": "sess-fallback"}) + "\n"]
        )
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.session_id == "sess-fallback"
        assert result.output == json.dumps({"type": "assistant", "session_id": "sess-fallback"})


class TestClaudePermissionMode:
    """Assert that --permission-mode is injected based on sandbox_mode."""

    def _popen_capture(self, mock_popen: MagicMock) -> list[str]:
        """Extract the cmd list from the Popen call."""
        return mock_popen.call_args[0][0]

    def test_workspace_write_adds_permission_mode(self, tmp_path: Path) -> None:
        """sandbox_mode=workspace-write → --permission-mode default in cmd."""
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=900,
            allowed_tools=("Read", "Edit", "Write", "Bash"),
            sandbox_mode="workspace-write",
        )
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        cmd = self._popen_capture(mock_popen)
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "default"

    def test_read_only_adds_permission_mode_with_warning(self, tmp_path: Path) -> None:
        """sandbox_mode=read-only → --permission-mode default + WARNING that read-only is not
        mechanically enforced by Claude CLI."""
        import theforge.runners.runner_claude as _mod

        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=900,
            allowed_tools=("Read", "Edit", "Write", "Bash"),
            sandbox_mode="read-only",
        )
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            with patch.object(_mod, "_log") as mock_log:
                run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        cmd = self._popen_capture(mock_popen)
        assert "--permission-mode" in cmd
        # A warning must be logged explaining that read-only is not mechanically enforced
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any(
            "read-only" in call and "not mechanically enforced" in call for call in log_calls
        )

    def test_none_omits_permission_mode(self, tmp_path: Path) -> None:
        """sandbox_mode=none → no --permission-mode flag in cmd."""
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=900,
            allowed_tools=("Read", "Edit", "Write", "Bash"),
            sandbox_mode="none",
        )
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        cmd = self._popen_capture(mock_popen)
        assert "--permission-mode" not in cmd


class TestClaudeSessionIdHelper:
    def test_get_claude_session_id_from_jsonl(self, tmp_path: Path) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "assistant", "session_id": "sess-from-jsonl"}),
                json.dumps({"type": "result", "result": "done"}),
            ]
        )

        sid = runner_claude_mod._get_claude_session_id(output, tmp_path)

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

        with patch("theforge.runners.runner_claude.Path.home", return_value=tmp_path):
            sid = runner_claude_mod._get_claude_session_id("", tmp_path, min_mtime=now - 10)

        assert sid == "sess-new"

    def test_get_claude_session_id_no_fallback_for_pool(self, tmp_path: Path) -> None:
        project_slug = str(tmp_path.resolve()).replace("/", "-")
        claude_dir = tmp_path / ".claude" / "projects" / project_slug
        claude_dir.mkdir(parents=True)
        (claude_dir / "sess-from-disk.jsonl").write_text("", encoding="utf-8")

        with patch("theforge.runners.runner_claude.Path.home", return_value=tmp_path):
            sid = runner_claude_mod._get_claude_session_id(
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
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd == 0.42
        assert isinstance(result.cost_usd, float)

    def test_null_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock([_result_line(result="done", cost_usd=None)])
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.cost_usd is None

    def test_garbage_cost_usd(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd="not-a-number")])
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
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
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
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
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
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
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
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


def test_claude_launcher_invokes_cmd_directly(dev_profile: ModelProfile, tmp_path: Path) -> None:
    mock_proc = _make_stream_mock([_result_line(result="done")])
    with patch(
        "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "claude"
