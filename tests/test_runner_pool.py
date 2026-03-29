"""Tests for the runner pool — TestRunAgentPool."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.agent_types import AgentResult
from theforge.config import ModelProfile
from theforge.runners import run_agent_pool


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

        with patch("theforge.runners.cli.run_agent", side_effect=mock_run_agent):
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
        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
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

        with patch("theforge.runners.cli.run_agent", side_effect=mock_run_agent):
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

        with patch("theforge.runners.cli.run_agent", side_effect=mock_run_agent):
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

        with patch("theforge.runners.cli.run_agent", side_effect=mock_run_agent):
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

        with patch("theforge.runners.cli.run_agent", side_effect=mock_run_agent):
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

        with patch("theforge.runners.cli.run_agent", side_effect=slow_agent):
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

        with patch("theforge.runners.cli.run_agent", side_effect=variable_agent):
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
            patch("theforge.runners.cli.run_agent", side_effect=mock_run_agent),
            patch("theforge.runners.cli.ThreadPoolExecutor") as mock_executor,
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

        with patch("theforge.runners.cli.run_agent", side_effect=flaky_agent):
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

        with patch("theforge.runners.cli.run_agent", side_effect=mock_run_agent):
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

        with patch("theforge.runners.cli.run_agent", side_effect=capture_prompt):
            run_agent_pool(prompt="the shared prompt", profiles=profiles, working_dir=tmp_path)

        assert len(received_prompts) == 2
        assert all(p == "the shared prompt" for p in received_prompts)
