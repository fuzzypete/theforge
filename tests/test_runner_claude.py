"""Tests for Claude CLI runner: hybrid dispatch, session helpers, cost coercion, model usage."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

import theforge.runners.runner_claude as runner_claude_mod
from theforge.agent_types import (
    COST_ESTIMATED,
    COST_PROVIDER_REPORTED,
    COST_UNKNOWN,
    AgentResult,
)
from theforge.config import ModelProfile
from theforge.config.types import StuckDetectionConfig
from theforge.log_level import LogLevel, set_log_level
from theforge.runners import run_agent, run_agent_pool
from theforge.runners.runner_claude import _run_claude


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


def _make_claude_profile(sandbox_mode: str = "workspace-write") -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=2.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash"),
        sandbox_mode=sandbox_mode,
    )


def _fake_wrap(cmd: list[str], working_dir: Path, **kwargs: object) -> list[str]:
    """Deterministic sandbox wrapper stub — prefixes a marker so tests do not
    depend on whether the host actually has sandbox-exec/bwrap available."""
    return ["sandbox-exec", "-p", "PROFILE", *cmd]


@pytest.fixture(autouse=True)
def _neutralize_sandbox_wrapper():
    """Claude lifecycle tests (with mocked Popen) exercise streaming/cost/session
    behavior, not sandbox containment (covered by TestClaudeSandboxWrapper and
    test_runner_sandbox.py). Neutralize the host sandbox wrapper with a
    deterministic sandbox-exec prefix so these tests do not fail closed on
    hosts/CI where sandbox-exec/bwrap is unavailable (the gate scrub can hide
    it). Real-subprocess tests (TestClaudeLifecycle) run with sandbox_mode=none
    so they exec the fake CLI directly; tests that assert wrapping override this
    with their own patch."""
    with patch(
        "theforge.runners.runner_claude.workspace_effect_sandbox_command",
        side_effect=_fake_wrap,
    ):
        yield


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
            progress_cb=None,
        )
        # The API dispatch fills the whole invocation identity, not just the
        # transport (#2205): a single-model API profile's resolved model is its
        # configured one, and the ledger needs both stated rather than one
        # recorded and the other left null.
        assert result == AgentResult(
            **{
                **mock_result.__dict__,
                "transport_used": "api",
                "model_used": "o4-mini",
                "configured_model": "o4-mini",
                "configured_transport": "api",
            }
        )

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
            progress_cb=None,
        )
        assert result == AgentResult(
            **{
                **mock_result.__dict__,
                "transport_used": "api",
                "model_used": "gpt-4o",
                "configured_model": "gpt-4o",
                "configured_transport": "api",
            }
        )

    def test_run_agent_cli_dispatch(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """run_agent dispatches to CLI runner for CLI profiles."""
        cli_result = AgentResult(
            success=True,
            output="cli result",
            session_id="sess-1",
            cost_usd=0.1,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        with patch(
            "theforge.runners.runner_claude._run_claude",
            return_value=cli_result,
        ) as mock_cli_run:
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

        # The runner uses --input-format stream-json and writes the initial
        # prompt as a stream-json user message; stdin is kept open across the
        # stream and closed only after the stream completes.
        assert "--input-format" in claude_cmd
        ifmt_idx = claude_cmd.index("--input-format")
        assert claude_cmd[ifmt_idx + 1] == "stream-json"
        write_calls = mock_proc.stdin.write.call_args_list
        assert len(write_calls) == 1
        written = write_calls[0][0][0]
        assert written.endswith("\n")
        payload = json.loads(written)
        assert payload["type"] == "user"
        assert payload["message"]["role"] == "user"
        assert payload["message"]["content"] == "implement the thing"
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

    def test_uses_workspace_venv_env(self, dev_profile: ModelProfile, tmp_path: Path) -> None:
        """Claude subprocess env prefers the worktree virtualenv."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        mock_proc = _make_stream_mock([_result_line(result="done")])

        with patch(
            "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            run_agent(
                prompt="test",
                profile=dev_profile,
                working_dir=tmp_path,
                secrets={"API_KEY": "secret"},
            )

        env_passed = mock_popen.call_args[1]["env"]
        assert env_passed["PATH"].split(os.pathsep)[0] == str(venv_bin)
        assert env_passed["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        assert env_passed["API_KEY"] == "secret"

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
        # A timeout is a distinct, retryable failure kind — not an
        # undifferentiated crash. The coordinator keys its retry decision off
        # failure_code, so the runner must classify it.
        assert result.failure_code == "timeout"

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
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "session_id": "sess-fallback",
                "message": {"content": [{"type": "text", "text": "agent text"}]},
            }
        )
        mock_proc = _make_stream_mock([assistant_line + "\n"])
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is True
        assert result.session_id == "sess-fallback"
        assert result.output == "agent text"

    def test_result_event_without_result_uses_last_assistant_text(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        assistant_line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "partial agent output"}]},
            }
        )
        mock_proc = _make_stream_mock(
            [
                assistant_line + "\n",
                _result_line(subtype="error_max_turns", session_id="sess-partial"),
            ],
            returncode=1,
        )
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.session_id == "sess-partial"
        assert result.output == "partial agent output"

    def test_result_event_without_result_emits_marker_when_no_assistant_text(
        self, dev_profile: ModelProfile, tmp_path: Path
    ) -> None:
        mock_proc = _make_stream_mock(
            [_result_line(subtype="error_max_turns", session_id="sess-empty")],
            returncode=1,
        )
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(prompt="test", profile=dev_profile, working_dir=tmp_path)

        assert result.success is False
        assert result.session_id == "sess-empty"
        assert (
            result.output == "CLAUDE_STREAM_NO_TEXT: reason=result_missing_text "
            "subtype=error_max_turns"
        )


class TestClaudePermissionMode:
    """Assert that --permission-mode is injected based on sandbox_mode."""

    def _popen_capture(self, mock_popen: MagicMock) -> list[str]:
        """Extract the cmd list from the Popen call."""
        return mock_popen.call_args[0][0]

    def test_workspace_write_adds_permission_mode(self, tmp_path: Path) -> None:
        """sandbox_mode=workspace-write → --permission-mode default in the (wrapped) cmd."""
        profile = _make_claude_profile("workspace-write")
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with (
            patch(
                "theforge.runners.runner_claude.workspace_effect_sandbox_command",
                side_effect=_fake_wrap,
            ),
            patch(
                "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
            ) as mock_popen,
        ):
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        cmd = self._popen_capture(mock_popen)
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "default"

    def test_read_only_adds_permission_mode_with_warning(self, tmp_path: Path) -> None:
        """sandbox_mode=read-only → --permission-mode default + WARNING that read-only is not
        mechanically enforced by Claude CLI."""
        import theforge.runners.runner_claude as _mod

        profile = _make_claude_profile("read-only")
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with (
            patch(
                "theforge.runners.runner_claude.workspace_effect_sandbox_command",
                side_effect=_fake_wrap,
            ),
            patch(
                "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
            ) as mock_popen,
            patch.object(_mod, "_log") as mock_log,
        ):
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        cmd = self._popen_capture(mock_popen)
        assert "--permission-mode" in cmd
        # A warning must be logged explaining that read-only is not mechanically enforced
        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any(
            "read-only" in call and "not mechanically enforced" in call for call in log_calls
        )

    def test_none_omits_permission_mode(self, tmp_path: Path) -> None:
        """sandbox_mode=none → no --permission-mode flag, and no sandbox wrapping."""
        profile = _make_claude_profile("none")
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with (
            patch(
                "theforge.runners.runner_claude.workspace_effect_sandbox_command",
                side_effect=_fake_wrap,
            ) as mock_wrap,
            patch(
                "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
            ) as mock_popen,
        ):
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        cmd = self._popen_capture(mock_popen)
        assert "--permission-mode" not in cmd
        mock_wrap.assert_not_called()
        assert cmd[0] == "claude"


class TestClaudeSandboxWrapper:
    """Assert the Claude CLI is mechanically wrapped / fails closed (#1907)."""

    def test_workspace_write_wraps_command(self, tmp_path: Path) -> None:
        """sandbox_mode != none → the raw claude argv is wrapped, not passed to Popen directly."""
        profile = _make_claude_profile("workspace-write")
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with (
            patch(
                "theforge.runners.runner_claude.workspace_effect_sandbox_command",
                side_effect=_fake_wrap,
            ) as mock_wrap,
            patch(
                "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
            ) as mock_popen,
        ):
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        mock_wrap.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        # Contained mode: Popen receives the wrapper, NOT the bare claude argv.
        assert cmd[0] == "sandbox-exec"
        assert cmd[0] != "claude"
        assert "claude" in cmd

    def test_wrapper_receives_credential_services_and_claude_state_roots(
        self, tmp_path: Path
    ) -> None:
        """The wrap grants keychain auth + ~/.claude writes so containment does not break auth."""
        profile = _make_claude_profile("workspace-write")
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with (
            patch(
                "theforge.runners.runner_claude.workspace_effect_sandbox_command",
                side_effect=_fake_wrap,
            ) as mock_wrap,
            patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc),
        ):
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        _, kwargs = mock_wrap.call_args
        assert kwargs["allow_credential_services"] is True
        write_roots = [str(p) for p in kwargs["extra_write_roots"]]
        assert any(p.endswith("/.claude") for p in write_roots)

    def test_read_only_also_wraps(self, tmp_path: Path) -> None:
        """read-only still gets host-wrapped for write containment to the worktree."""
        profile = _make_claude_profile("read-only")
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with (
            patch(
                "theforge.runners.runner_claude.workspace_effect_sandbox_command",
                side_effect=_fake_wrap,
            ) as mock_wrap,
            patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc),
        ):
            run_agent(prompt="test", profile=profile, working_dir=tmp_path)
        mock_wrap.assert_called_once()

    def test_sandbox_unavailable_fails_closed(self, tmp_path: Path) -> None:
        """Host sandbox unavailable → runner fails closed without launching claude."""
        profile = _make_claude_profile("workspace-write")
        with (
            patch(
                "theforge.runners.runner_claude.workspace_effect_sandbox_command",
                side_effect=lambda cmd, wd, **kw: list(cmd),
            ),
            patch("theforge.runners.runner_claude.subprocess.Popen") as mock_popen,
        ):
            result = _run_claude(prompt="test", profile=profile, working_dir=tmp_path)
        # Claude must NOT be launched — we fail before Popen.
        mock_popen.assert_not_called()
        assert result.success is False
        assert result.startup_failure is True
        assert result.exit_code == -1
        assert "SANDBOX_UNAVAILABLE" in result.output

    def test_none_mode_runs_unwrapped(self, tmp_path: Path) -> None:
        """sandbox_mode=none → no wrapping; the bare claude argv is launched."""
        profile = _make_claude_profile("none")
        mock_proc = _make_stream_mock([_result_line(result="done", total_cost_usd=0.01)])
        with (
            patch("theforge.runners.runner_claude.workspace_effect_sandbox_command") as mock_wrap,
            patch(
                "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
            ) as mock_popen,
        ):
            result = _run_claude(prompt="test", profile=profile, working_dir=tmp_path)
        mock_wrap.assert_not_called()
        mock_popen.assert_called_once()
        assert mock_popen.call_args[0][0][0] == "claude"
        assert result.success is True


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


def _assistant_usage_line(
    *,
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> str:
    """Build a stream-json assistant event carrying a per-message usage block."""
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": model,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": cache_creation,
                    },
                },
            }
        )
        + "\n"
    )


class TestReconstructPartialCost:
    """Unit tests for kill-path cost reconstruction from partial stream usage."""

    def test_sums_and_prices_assistant_usage(self, dev_profile: ModelProfile) -> None:
        lines = [
            _assistant_usage_line(input_tokens=1000, output_tokens=500),
            _assistant_usage_line(input_tokens=2000, output_tokens=1000, cache_read=10000),
        ]
        cost, usage = runner_claude_mod._reconstruct_partial_cost(lines, dev_profile)
        # sonnet pricing: input 3.00/Mtok, output 15.00/Mtok, cache_read 0.1x input.
        expected = (
            (3000 / 1_000_000) * 3.00
            + (1500 / 1_000_000) * 15.00
            + (10000 / 1_000_000) * 3.00 * 0.1
        )
        assert cost is not None
        assert cost == pytest.approx(expected)
        assert cost > 0.0
        assert len(usage) == 1
        assert usage[0].input_tokens == 3000
        assert usage[0].output_tokens == 1500
        assert usage[0].cache_read_tokens == 10000

    def test_no_usage_returns_none_not_zero(self, dev_profile: ModelProfile) -> None:
        lines = [json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"]
        cost, usage = runner_claude_mod._reconstruct_partial_cost(lines, dev_profile)
        assert cost is None
        assert usage == ()

    def test_unpriced_model_records_usage_but_unknown_cost(
        self, dev_profile: ModelProfile
    ) -> None:
        lines = [_assistant_usage_line(model="mystery-model-9", input_tokens=1000)]
        cost, usage = runner_claude_mod._reconstruct_partial_cost(lines, dev_profile)
        assert cost is None
        assert len(usage) == 1
        assert usage[0].cost_usd is None

    def test_dated_model_id_resolves_to_family_pricing(self, dev_profile: ModelProfile) -> None:
        lines = [_assistant_usage_line(model="claude-sonnet-4-6-20260115", input_tokens=1000)]
        cost, _ = runner_claude_mod._reconstruct_partial_cost(lines, dev_profile)
        assert cost is not None
        assert cost == pytest.approx((1000 / 1_000_000) * 3.00)


class TestTimeoutCostReconstruction:
    """A run killed at the deadline attributes cost from partial stream usage."""

    def _blocking_after(self, usage_lines: list[str]):
        class _PartialThenBlockStdout:
            def __iter__(self):
                yield from usage_lines
                time.sleep(0.5)  # longer than the tiny timeout
                return

        return _PartialThenBlockStdout()

    def _tiny_timeout_profile(self) -> ModelProfile:
        return ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=0.1,
            allowed_tools=(),
        )

    def test_timeout_reconstructs_nonzero_cost(self, tmp_path: Path) -> None:
        usage_lines = [
            _assistant_usage_line(input_tokens=5000, output_tokens=2000, cache_read=40000),
        ]
        mock_proc = MagicMock()
        mock_proc.stdout = self._blocking_after(usage_lines)
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.returncode = -1
        mock_proc.wait.return_value = -1
        mock_proc.poll.return_value = None

        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(
                prompt="test", profile=self._tiny_timeout_profile(), working_dir=tmp_path
            )

        assert result.exit_code == -9
        assert "TIMEOUT" in result.output
        # The whole point: a killed run's cost is reconstructed, not dropped to 0.0.
        assert result.cost_usd is not None
        assert result.cost_usd > 0.0
        assert len(result.model_usage) == 1

    def test_timeout_without_usage_records_unknown_not_zero(self, tmp_path: Path) -> None:
        no_usage_line = json.dumps({"type": "assistant", "session_id": "s"}) + "\n"
        mock_proc = MagicMock()
        mock_proc.stdout = self._blocking_after([no_usage_line])
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.returncode = -1
        mock_proc.wait.return_value = -1
        mock_proc.poll.return_value = None

        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = run_agent(
                prompt="test", profile=self._tiny_timeout_profile(), working_dir=tmp_path
            )

        assert result.exit_code == -9
        # Unmeasurable cost stays explicitly unknown (None), never a fabricated 0.0.
        assert result.cost_usd is None


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


def test_claude_launcher_invokes_cmd_directly_when_sandbox_none(tmp_path: Path) -> None:
    """sandbox_mode=none is the only mode that launches the bare claude argv (#1907).

    With containment requested the launcher is wrapped instead — see
    TestClaudeSandboxWrapper.
    """
    profile = _make_claude_profile("none")
    mock_proc = _make_stream_mock([_result_line(result="done")])
    with patch(
        "theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        run_agent(prompt="test", profile=profile, working_dir=tmp_path)
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "claude"


# ---------------------------------------------------------------------------
# Lifecycle tests — real subprocess, fake-CLI fixture
# ---------------------------------------------------------------------------


class TestClaudeLifecycle:
    """Real-subprocess lifecycle tests for the Claude CLI runner.

    Uses tests/fake_bin/claude rather than mocking subprocess.Popen, so the
    tests exercise real pipe semantics (stdin/stdout EOF, process exit timing,
    watchdog behaviour) that Popen mocks cannot detect.

    The fake binary is controlled by the FAKE_CLAUDE_MODE env var injected
    via a monkeypatched build_workspace_env.
    """

    _FAKE_BIN: ClassVar[Path] = Path(__file__).parent / "fake_bin"

    @pytest.fixture(autouse=True)
    def _ensure_executable(self) -> None:
        script = self._FAKE_BIN / "claude"
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _patch_env(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        """Patch runner_claude.build_workspace_env to put fake_bin first on PATH."""
        fake_bin = self._FAKE_BIN
        from theforge.workspace_env import build_workspace_env as _orig

        def _build(
            workspace_path: Path,
            base_env: object = None,
            *,
            extra: object = None,
        ) -> dict:
            env = _orig(workspace_path, base_env, extra=extra)  # type: ignore[arg-type]
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env["FAKE_CLAUDE_MODE"] = mode
            return env

        monkeypatch.setattr("theforge.runners.runner_claude.build_workspace_env", _build)

    def _make_profile(self, timeout_seconds: int = 10, **kwargs: object) -> ModelProfile:
        # sandbox_mode=none: these real-subprocess tests exec the fake CLI
        # directly. Host sandbox wrapping is covered by TestClaudeSandboxWrapper.
        kwargs.setdefault("sandbox_mode", "none")
        return ModelProfile(
            name="dev",
            cli="claude",
            model="claude-sonnet-4-5",
            budget_usd=2.0,
            timeout_seconds=timeout_seconds,
            allowed_tools=("Bash",),
            **kwargs,  # type: ignore[arg-type]
        )

    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Fake claude emits a complete result event; runner returns success."""
        self._patch_env(monkeypatch, "happy")
        result = _run_claude(
            prompt="do the thing",
            profile=self._make_profile(),
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.success is True
        assert result.output == "Task complete."
        assert result.session_id == "fake-session-abc123"
        assert result.exit_code == 0

    def test_reported_cost_is_marked_provider_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A cost the CLI's result event carried is recorded as billed, not derived (#2205).

        Run through ``run_agent`` rather than ``_run_claude`` so the configured
        half of the ledger is stamped too — the two are one contract and the
        seam that fills them is the dispatch, not the runner.
        """
        self._patch_env(monkeypatch, "happy")
        profile = self._make_profile()

        result = run_agent(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.success is True
        assert result.cost_usd == 0.001
        assert result.cost_provenance == COST_PROVIDER_REPORTED
        assert result.configured_model == "claude-sonnet-4-5"
        assert result.configured_transport == "cli"
        assert result.transport_used == "cli"

    def test_kill_path_reconstructed_cost_is_marked_estimated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Spend rebuilt from the stream is forge's pricing, and says so (#2205).

        The CLI never reported a figure here — it was killed before its result
        event — so the number is a pricing-table derivation. Recording it with
        the same provenance as a billed one would make the two unattributable.
        """
        self._patch_env(monkeypatch, "usage_then_hang")
        profile = self._make_profile(timeout_seconds=2)

        result = run_agent(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.success is False
        assert result.cost_usd is not None and result.cost_usd > 0
        assert result.cost_provenance == COST_ESTIMATED
        assert [u.cost_provenance for u in result.model_usage] == [COST_ESTIMATED]
        assert result.model_usage[0].input_tokens == 1000
        assert result.model_usage[0].cache_read_tokens == 500

    def test_unmeasured_cost_is_unknown_not_estimated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A run killed before emitting any usage claims no provenance at all."""
        self._patch_env(monkeypatch, "hang")
        profile = self._make_profile(timeout_seconds=2)

        result = run_agent(prompt="do the thing", profile=profile, working_dir=tmp_path)

        assert result.cost_usd is None
        assert result.cost_provenance == COST_UNKNOWN

    def test_stdin_close_no_hang(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Runner closes stdin after stdout closes; subprocess exits without timing out.

        Regression test for #1054: before the fix, stdin was never closed so a
        subprocess that waits for stdin-EOF before exiting would deadlock until
        the watchdog fired, producing a timeout failure even when a complete
        result had been emitted.
        """
        self._patch_env(monkeypatch, "wait_stdin")
        profile = self._make_profile(timeout_seconds=5)
        start = time.monotonic()
        result = _run_claude(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        elapsed = time.monotonic() - start
        assert result.success is True, f"expected success; got output={result.output!r}"
        assert result.output == "Task complete."
        assert elapsed < 4.0, (
            f"runner took {elapsed:.2f}s — likely stdin-close deadlock regression (#1054)"
        )

    def test_wall_clock_timeout_no_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Subprocess that never produces output is killed by watchdog; runner returns failure."""
        self._patch_env(monkeypatch, "hang")
        profile = self._make_profile(timeout_seconds=2)
        start = time.monotonic()
        result = _run_claude(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        elapsed = time.monotonic() - start
        assert result.success is False
        assert elapsed < 5.0, f"runner took {elapsed:.2f}s — watchdog did not fire in time"

    def test_silent_stream_close_is_classified_as_never_ran(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The #2832 shape: the CLI emits nothing, and the runner kills it at the exit grace.

        This is the confirmed mechanism behind every occurrence in that issue —
        three post-gate-failure DEV invocations and one ``forge diagnose`` run,
        all ``exit=-9``/``$0.000``/no output within 11-13s of starting. The CLI's
        stdout reaches EOF without a single stream event; the runner closes stdin,
        waits out ``_EXIT_GRACE_SECONDS``, finds the process still alive and
        SIGKILLs its own group. Before the fix that result was reported as
        ``agent_ended_without_result`` with ``cost_usd=None`` — identical in the
        record to an agent that had worked and gone quiet, and (because
        cost-unknown fails ``zero_charge_no_model_artifacts``) charged to the
        story's retry allowance.

        The timing assertion is the load-bearing half: it pins the kill to the
        exit grace rather than to the profile timeout, which is what makes this a
        regression test for the mechanism and not just for the exit code.
        """
        self._patch_env(monkeypatch, "silent_stdout_close")
        # A long profile timeout on purpose: if the classification ever starts
        # depending on the timeout path, this test hangs instead of passing.
        profile = self._make_profile(timeout_seconds=3600)
        start = time.monotonic()
        result = _run_claude(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        elapsed = time.monotonic() - start

        assert result.success is False
        assert result.failure_code == "killed_before_output"
        assert result.exit_code == -9
        # Measured zero, not unknown. This is the field the coordinator's
        # zero_charge_no_model_artifacts() predicate turns on, and the reason the
        # retry slot is released rather than spent.
        assert result.cost_usd == 0.0
        assert result.session_id is None
        assert result.model_usage == ()
        assert not result.tool_trace
        assert "KILLED_BEFORE_OUTPUT" in result.output
        assert runner_claude_mod._EXIT_GRACE_SECONDS <= elapsed < 30.0, (
            f"killed after {elapsed:.1f}s — expected the post-stream exit grace "
            f"({runner_claude_mod._EXIT_GRACE_SECONDS}s), not the profile timeout"
        )

    def test_never_ran_result_releases_the_retry_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The classified result satisfies the coordinator predicate that refunds a retry.

        Asserted here, against a real subprocess result rather than a hand-built
        one, because the whole point of #2832's third criterion is that *this*
        shape reaches the refund. A unit test over a synthetic AgentResult would
        pass even if the runner stopped producing the shape.
        """
        from theforge.coordinator.agent_failure import (
            classify_failure_category,
            produced_model_output,
            zero_charge_no_model_artifacts,
        )

        self._patch_env(monkeypatch, "silent_stdout_close")
        result = _run_claude(
            prompt="do the thing",
            profile=self._make_profile(timeout_seconds=3600),
            working_dir=tmp_path,
            fallback_to_file=False,
        )

        assert zero_charge_no_model_artifacts(result) is True
        assert produced_model_output(result) is False
        # A process fact, not "the agent ran and reported nothing".
        assert classify_failure_category(result) == "process"

    def test_hang_until_timeout_is_not_classified_as_never_ran(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A timeout kill keeps its own code even though it too produced no output.

        The distinction #2832 draws is not "did it print anything" — it is
        whether the invocation was given and used the story's allowance. A
        timeout spent the full allowance and is evidence about the story, so it
        must not release a retry slot; ``killed_before_output`` must not swallow
        it.
        """
        self._patch_env(monkeypatch, "hang")
        result = _run_claude(
            prompt="do the thing",
            profile=self._make_profile(timeout_seconds=2),
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.success is False
        assert result.failure_code == "timeout"
        assert result.failure_code != "killed_before_output"

    def test_streamed_usage_then_kill_is_not_classified_as_never_ran(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An invocation that streamed real usage before dying did run."""
        self._patch_env(monkeypatch, "usage_then_hang")
        result = _run_claude(
            prompt="do the thing",
            profile=self._make_profile(timeout_seconds=2),
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.failure_code != "killed_before_output"

    def test_nudge_delivery(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Runner writes stuck-detection nudge to stdin; subprocess receives it and responds."""
        self._patch_env(monkeypatch, "nudge")
        profile = self._make_profile(
            timeout_seconds=10,
            phase="dev",
            stuck_detection=StuckDetectionConfig(
                enabled=True,
                repeat_threshold=2,
                post_nudge_iterations=10,
                no_progress_iterations=10,
                error_threshold=10,
            ),
        )
        result = _run_claude(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.success is True
        assert result.output == "Task complete."
