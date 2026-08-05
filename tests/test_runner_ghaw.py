"""Tests for the gh-aw (GitHub Agentic Workflows) runner.

Lifecycle tests use the tests/fake_bin/gh subprocess fixture (per
runners/CONVENTIONS.md) rather than mocking subprocess.run, so real
process/exit behaviour is exercised across the dispatch → discover →
poll → download sequence.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from theforge.config import ModelProfile
from theforge.runners import runner_ghaw
from theforge.runners.cli import run_agent
from theforge.runners.runner_ghaw import (
    _run_ghaw,
    build_argv,
    build_run_download_argv,
    build_run_list_argv,
    build_run_view_argv,
)


def _make_profile(timeout_seconds: int = 30) -> ModelProfile:
    return ModelProfile(
        name="dev-ghaw",
        cli="ghaw",
        model="copilot",
        budget_usd=2.0,
        timeout_seconds=timeout_seconds,
        allowed_tools=(),
    )


class TestArgvBuilders:
    def test_dispatch_argv_shape(self) -> None:
        argv = build_argv(
            workflow="forge-dev-ghaw.lock.yml",
            ref="feature-branch",
            dispatch_id="abc123",
            prompt="do the thing",
            story_ref="1680",
        )
        assert argv[:4] == ["gh", "workflow", "run", "forge-dev-ghaw.lock.yml"]
        assert "--ref" in argv and argv[argv.index("--ref") + 1] == "feature-branch"
        assert "dispatch_id=abc123" in argv
        assert "story_ref=1680" in argv
        assert "prompt=do the thing" in argv

    def test_poll_and_download_argv_shapes(self) -> None:
        assert build_run_list_argv(workflow="w.lock.yml", ref="main")[:3] == [
            "gh",
            "run",
            "list",
        ]
        assert build_run_view_argv(run_id="42")[:4] == ["gh", "run", "view", "42"]
        argv = build_run_download_argv(run_id="42", dest="/tmp/x")
        assert argv[:4] == ["gh", "run", "download", "42"]
        assert argv[argv.index("--dir") + 1] == "/tmp/x"


class TestGhawLifecycle:
    """Subprocess lifecycle tests against tests/fake_bin/gh."""

    _FAKE_BIN: ClassVar[Path] = Path(__file__).parent / "fake_bin"

    @pytest.fixture(autouse=True)
    def _fake_gh(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        fake_gh = self._FAKE_BIN / "gh"
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("FAKE_GH_STATE_DIR", str(tmp_path / "gh-state"))
        (tmp_path / "gh-state").mkdir()
        monkeypatch.setenv("THEFORGE_GHAW_POLL_SECONDS", "0.01")

        def _env_with_fake_bin(working_dir, extra=None):
            env = dict(os.environ)
            if extra:
                env.update(extra)
            env["PATH"] = str(self._FAKE_BIN) + os.pathsep + env.get("PATH", "")
            return env

        monkeypatch.setattr(runner_ghaw, "build_workspace_env", _env_with_fake_bin)

    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("FAKE_GH_MODE", "happy")
        result = _run_ghaw(prompt="implement", profile=_make_profile(), working_dir=tmp_path)
        assert result.success is True
        assert "Fake ghaw agent output" in result.output
        assert result.session_id == "4242"
        assert result.cost_usd == pytest.approx(0.22479)  # 22.479 AIC × $0.01
        assert result.model_usage[0].model == "gpt-4.1-2025-04-14"
        assert result.model_usage[0].input_tokens == 25965
        assert result.raw["run"]["conclusion"] == "success"
        assert result.raw["timing"]["total_seconds"] >= 0
        assert "agent-output/agent-output.md" in result.raw["artifacts"]

    def test_dispatch_failure_is_startup_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("FAKE_GH_MODE", "dispatch_fail")
        result = _run_ghaw(prompt="implement", profile=_make_profile(), working_dir=tmp_path)
        assert result.success is False
        assert result.startup_failure is True
        assert "DISPATCH_FAILED" in result.output

    def test_run_conclusion_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("FAKE_GH_MODE", "run_fail")
        result = _run_ghaw(prompt="implement", profile=_make_profile(), working_dir=tmp_path)
        assert result.success is False
        assert result.exit_code == 1
        assert result.raw["run"]["conclusion"] == "failure"

    def test_wall_clock_timeout_cancels_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("FAKE_GH_MODE", "never_completes")
        result = _run_ghaw(
            prompt="implement",
            profile=_make_profile(timeout_seconds=1),
            working_dir=tmp_path,
        )
        assert result.success is False
        assert result.exit_code == -1
        assert "TIMEOUT" in result.output

    def test_lost_dispatch_reports_run_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("FAKE_GH_MODE", "list_empty")
        result = _run_ghaw(
            prompt="implement",
            profile=_make_profile(timeout_seconds=1),
            working_dir=tmp_path,
        )
        assert result.success is False
        assert "RUN_NOT_FOUND" in result.output


class TestGhawGuards:
    def test_oversized_prompt_refused_before_dispatch(self, tmp_path: Path) -> None:
        result = _run_ghaw(
            prompt="x" * 70_000,
            profile=_make_profile(),
            working_dir=tmp_path,
        )
        assert result.success is False
        assert result.startup_failure is True
        assert "PROMPT_TOO_LARGE" in result.output

    def test_run_agent_routes_ghaw_profile(self, tmp_path: Path) -> None:
        with patch("theforge.runners.runner_ghaw._run_ghaw") as mock_run:
            mock_run.return_value = _make_result()
            result = run_agent(
                prompt="implement",
                profile=_make_profile(),
                working_dir=tmp_path,
            )
        assert mock_run.call_count == 1
        assert result.model_used == "copilot"
        # The model back-fill without a transport is only half an identity: a
        # bare model name cannot be canonicalized without knowing which
        # transport served it (#2225). gh-aw dispatches a CLI engine.
        assert result.transport_used == "cli"


def _make_result():
    from theforge.agent_types import AgentResult

    return AgentResult(
        success=True,
        output="ok",
        session_id="1",
        cost_usd=None,
        exit_code=0,
        raw={},
        profile_name="dev-ghaw",
    )
