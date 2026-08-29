"""Tests for the agent-ended-without-result seam (#2427).

A dev agent that delegates to a subagent, announces it will wait to be notified,
and then goes silent has *stated* how its run ended. Its process is killed
without ever emitting a terminal result event, and the stream it produced holds
those last words.

This file follows that fact across the three boundaries it used to fall through:

    runner (failure_code)  ──▶  dev phase (state.error / telemetry)
                           ──▶  sprint RCA (classification + recommendation)

The property under test is the same at each: an ending the run recorded is
reported as recorded, and never routed to a paid investigation that would only
rediscover it.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from theforge.agent_types import FAILURE_ENDED_WITHOUT_RESULT, AgentResult  # noqa: E402
from theforge.cli import sprint_digest  # noqa: E402
from theforge.config import (  # noqa: E402
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import _serialize_dev_iteration_metrics  # noqa: E402
from theforge.coordinator.dev_phase import (  # noqa: E402
    ENDED_WITHOUT_RESULT_PHRASE,
    _describe_dev_failure,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase  # noqa: E402
from theforge.runners import run_agent  # noqa: E402
from theforge.sprint.rca import RULES_BY_ID, UNKNOWN_CLASS, build_sprint_rca  # noqa: E402
from theforge.task import TaskStory  # noqa: E402

# The agent's literal final output in the reported run (issue-2365).
_LAST_WORDS = (
    "I'll wait for the exploration agent's findings before proceeding — no need to poll, "
    "I'll be notified when it completes."
)


# ── Runner: the ending gets a name ────────────────────────────────────────────


def _make_stream_mock(lines: list[str], returncode: int = 0, stderr: str = "") -> MagicMock:
    mock_proc = MagicMock()
    mock_proc.stdout = iter(lines)
    mock_proc.stdin = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = stderr
    mock_proc.returncode = returncode
    mock_proc.wait.return_value = returncode
    return mock_proc


def _assistant_line(text: str) -> str:
    return (
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        + "\n"
    )


def _dev_profile() -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=2.0,
        timeout_seconds=900,
        allowed_tools=(),
        sandbox_mode="none",
    )


def test_signal_killed_stream_without_result_event_is_named(tmp_path: Path) -> None:
    """A killed CLI that never emitted a result event carries a failure code."""
    proc = _make_stream_mock([_assistant_line(_LAST_WORDS)], returncode=-9)
    with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=proc):
        result = run_agent(prompt="test", profile=_dev_profile(), working_dir=tmp_path)

    assert result.success is False
    assert result.exit_code == -9
    assert result.failure_code == FAILURE_ENDED_WITHOUT_RESULT
    # The agent's own last words survive the kill — this is the text every
    # surface downstream was missing.
    assert result.output == _LAST_WORDS
    assert result.partial_output == _LAST_WORDS


def test_clean_exit_without_result_event_is_not_a_failure(tmp_path: Path) -> None:
    """Exit 0 with no result event is not a failure and gets no failure code."""
    proc = _make_stream_mock([_assistant_line("done")], returncode=0)
    with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=proc):
        result = run_agent(prompt="test", profile=_dev_profile(), working_dir=tmp_path)

    assert result.success is True
    assert result.failure_code is None


# ── Dev phase: the recorded ending reaches the operator-facing error ──────────


def _no_result_agent_result() -> AgentResult:
    return AgentResult(
        success=False,
        output=_LAST_WORDS,
        session_id="sess-abc",
        cost_usd=0.2089293,
        exit_code=-9,
        raw={},
        profile_name="dev",
        failure_code=FAILURE_ENDED_WITHOUT_RESULT,
        partial_output=_LAST_WORDS,
    )


def test_describe_dev_failure_quotes_the_agents_last_words() -> None:
    detail = _describe_dev_failure(_no_result_agent_result(), is_timeout=False)

    assert _LAST_WORDS in detail
    assert ENDED_WITHOUT_RESULT_PHRASE in detail
    # The exit status is kept — it is a real fact — but it is no longer the
    # whole of what the operator is given.
    assert detail != "exit=-9"
    assert "exit=-9" in detail


def test_describe_dev_failure_truncates_long_agent_text() -> None:
    long_text = "x" * 900
    result = AgentResult(
        success=False,
        output=long_text,
        session_id=None,
        cost_usd=None,
        exit_code=-9,
        raw={},
        profile_name="dev",
        failure_code=FAILURE_ENDED_WITHOUT_RESULT,
        partial_output=long_text,
    )

    detail = _describe_dev_failure(result, is_timeout=False)

    assert len(detail) < len(long_text)
    assert detail.endswith("…")


def test_describe_dev_failure_keeps_exit_code_when_nothing_was_said() -> None:
    """A no-result failure is still named even when the stream captured no agent text."""
    result = AgentResult(
        success=False,
        output="CLAUDE_STREAM_NO_TEXT: reason=missing_result_event",
        session_id=None,
        cost_usd=None,
        exit_code=-9,
        raw={},
        profile_name="dev",
        failure_code=FAILURE_ENDED_WITHOUT_RESULT,
    )

    assert _describe_dev_failure(result, is_timeout=False) == (
        f"exit=-9: the agent {ENDED_WITHOUT_RESULT_PHRASE}"
    )


def test_describe_dev_failure_unnamed_failure_still_reads_exit_code() -> None:
    result = AgentResult(
        success=False,
        output="",
        session_id=None,
        cost_usd=None,
        exit_code=-2,
        raw={},
        profile_name="dev",
    )

    assert _describe_dev_failure(result, is_timeout=False) == "exit=-2"


# ── Seam: dev phase → audit records → sprint RCA ──────────────────────────────


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    (path / ".gitignore").write_text(".forge/\n")
    # The story spec is committed so the worktree is clean when dev starts: the
    # iteration under test changed nothing, and a stray file would make the
    # run's own records say otherwise.
    (path / "specs").mkdir(parents=True, exist_ok=True)
    (path / "specs" / "t.md").write_text("# t\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _run_dev_once(tmp_path: Path, agent_result: AgentResult) -> CoordinatorState:
    from theforge.coordinator.dev_phase import _run_dev_phase

    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="issue-2365", story_path="specs/t.md")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 1
    state.budget.consume(review_cycle=0)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", return_value=agent_result)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None
        )
    assert isinstance(result, CoordinatorResult)
    assert state.phase == Phase.ESCALATE
    return state


def test_dev_phase_records_the_ending_the_runner_named(tmp_path: Path) -> None:
    """state.error — the audit's outcome.message — carries the agent's words."""
    state = _run_dev_once(tmp_path, _no_result_agent_result())

    assert _LAST_WORDS in (state.error or "")
    assert ENDED_WITHOUT_RESULT_PHRASE in (state.error or "")
    # And the run's per-iteration telemetry names the ending mechanically, so a
    # consumer never has to parse the sentence to learn it.
    assert state.dev_iteration_telemetry[-1].runner_failure_code == (FAILURE_ENDED_WITHOUT_RESULT)


def _sprint_artifacts(tmp_path: Path, state: CoordinatorState) -> Path:
    """Write summary + per-story audit from what the dev phase actually recorded."""
    sprint_dir = tmp_path / ".forge" / "logs" / "issues-2365"
    (sprint_dir / "issue-2365").mkdir(parents=True, exist_ok=True)
    summary = {
        "sprint": {
            "name": "test-sprint",
            "run_id": "743d1bdb3ee4",
            "finished_at": "2026-08-12T03:00:00Z",
        },
        "stories": [
            {
                "slug": "issue-2365",
                "outcome": "ESCALATE",
                "error": state.error,
                "cost_usd": 3.86119785,
            }
        ],
    }
    audit = {
        "outcome": {
            "success": False,
            "final_phase": "ESCALATE",
            "message": state.error,
            "error_type": None,
        },
        "iterations": {"dev_loop": _serialize_dev_iteration_metrics(state)},
        "cost": {
            "total_usd": 3.86119785,
            "dev_usd": 0.2089293,
            "dev_invocations": 1,
        },
        "workspace": {"path": str(tmp_path), "branch": "feat/x"},
    }
    (sprint_dir / "sprint-summary.yaml").write_text(yaml.safe_dump(summary), encoding="utf-8")
    (sprint_dir / "issue-2365" / "audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")
    return sprint_dir


def test_rca_classifies_the_recorded_ending_and_buys_no_investigation(tmp_path: Path) -> None:
    """The reported run's shape, end to end: no unknown class, no paid diagnosis."""
    state = _run_dev_once(tmp_path, _no_result_agent_result())
    sprint_dir = _sprint_artifacts(tmp_path, state)

    payload = build_sprint_rca(sprint_dir / "sprint-summary.yaml")
    entry = payload["stories"]["issue-2365"]

    assert entry["primary_failure_class"] == "agent_ended_without_result"
    assert entry["primary_failure_class"] != UNKNOWN_CLASS
    actions = entry["recommended_next_actions"]
    assert not any("forge diagnose" in action for action in actions)
    # The operator reads why the run ended without opening the dev log.
    excerpts = " ".join(item["excerpt"] for item in entry["evidence"])
    assert _LAST_WORDS in excerpts
    assert any(item["rule_id"] == "dev_agent_ended_without_result" for item in entry["evidence"])


def test_rca_reports_spend_on_preparation_for_work_that_never_began(tmp_path: Path) -> None:
    """Money spent and work attempted are reported as the different facts they are."""
    state = _run_dev_once(tmp_path, _no_result_agent_result())
    sprint_dir = _sprint_artifacts(tmp_path, state)

    entry = build_sprint_rca(sprint_dir / "sprint-summary.yaml")["stories"]["issue-2365"]

    shape = entry["spend_shape"]
    assert round(shape["total_usd"], 2) == 3.86
    assert round(shape["dev_usd"], 2) == 0.21
    assert round(shape["before_dev_usd"], 2) == 3.65
    assert "preparation" in shape["note"]
    # A dev iteration that changed nothing is not partial value to reuse.
    assert not any("iteration(s) of work" in value for value in entry["partial_value"])


def test_rca_falls_back_to_the_stated_ending_without_iteration_telemetry(
    tmp_path: Path,
) -> None:
    """A run whose per-iteration telemetry is gone still reads its own sentence."""
    sprint_dir = tmp_path / ".forge" / "logs" / "issues-2365"
    (sprint_dir / "issue-2365").mkdir(parents=True, exist_ok=True)
    error = (
        f"Dev agent failed (exit=-9: the agent {ENDED_WITHOUT_RESULT_PHRASE}; "
        f"it last said: {_LAST_WORDS}) and produced no commits ahead of base — "
        "escalating to avoid an empty-diff APPROVE"
    )
    summary = {
        "sprint": {"name": "s", "run_id": "r1", "finished_at": "2026-08-12T03:00:00Z"},
        "stories": [{"slug": "issue-2365", "outcome": "ESCALATE", "error": error}],
    }
    (sprint_dir / "sprint-summary.yaml").write_text(yaml.safe_dump(summary), encoding="utf-8")

    entry = build_sprint_rca(sprint_dir / "sprint-summary.yaml")["stories"]["issue-2365"]

    assert entry["primary_failure_class"] == "agent_ended_without_result"
    assert not any("forge diagnose" in a for a in entry["recommended_next_actions"])


def test_recovered_earlier_iteration_does_not_name_the_story(tmp_path: Path) -> None:
    """Only the terminal dev iteration's ending classifies the story."""
    sprint_dir = tmp_path / ".forge" / "logs" / "issues-2365"
    (sprint_dir / "issue-2365").mkdir(parents=True, exist_ok=True)
    summary = {
        "sprint": {"name": "s", "run_id": "r1", "finished_at": "2026-08-12T03:00:00Z"},
        "stories": [
            {
                "slug": "issue-2365",
                "outcome": "ESCALATE",
                "error": "review requested changes",
                "iteration_usage": {"dev": {"used": 3, "max": 3}},
            }
        ],
    }
    audit = {
        "outcome": {"success": False, "final_phase": "ESCALATE", "message": "escalated"},
        "iterations": {
            "dev_loop": [
                {
                    "iteration": 1,
                    "runner_failure_code": FAILURE_ENDED_WITHOUT_RESULT,
                    "runner_failure_summary": _LAST_WORDS,
                    "files_changed_count": 0,
                },
                {"iteration": 2, "runner_failure_code": None, "files_changed_count": 4},
            ]
        },
    }
    (sprint_dir / "sprint-summary.yaml").write_text(yaml.safe_dump(summary), encoding="utf-8")
    (sprint_dir / "issue-2365" / "audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")

    entry = build_sprint_rca(sprint_dir / "sprint-summary.yaml")["stories"]["issue-2365"]

    assert entry["primary_failure_class"] != "agent_ended_without_result"


def test_ending_without_result_rule_is_declared_and_greppable() -> None:
    rule = RULES_BY_ID["dev_agent_ended_without_result"]
    assert rule.failure_class == "agent_ended_without_result"
    assert rule.role == "primary"
    # Field-derived, never a text scan over agent prose.
    assert rule.patterns == ()


def test_unrelated_failure_still_falls_to_the_residual_class(tmp_path: Path) -> None:
    """The new class does not widen: an ending with no recorded cause is unknown."""
    sprint_dir = tmp_path / ".forge" / "logs" / "issues-99"
    (sprint_dir / "issue-99").mkdir(parents=True, exist_ok=True)
    summary = {
        "sprint": {"name": "s", "run_id": "r1", "finished_at": "2026-08-12T03:00:00Z"},
        "stories": [{"slug": "issue-99", "outcome": "ESCALATE", "error": "exit=-2"}],
    }
    (sprint_dir / "sprint-summary.yaml").write_text(yaml.safe_dump(summary), encoding="utf-8")

    entry = build_sprint_rca(sprint_dir / "sprint-summary.yaml")["stories"]["issue-99"]

    assert entry["primary_failure_class"] == UNKNOWN_CLASS


# ── Digest: the operator surface shows both facts ─────────────────────────────


def test_digest_prints_the_spend_split_beside_the_classification(capsys) -> None:
    entry = {
        "primary_failure_class": "agent_ended_without_result",
        "spend_shape": {
            "note": "$3.86 was spent on this story and $3.65 of it went to preparation"
        },
    }

    sprint_digest._print_entry_notes(entry)

    out = capsys.readouterr().out
    assert "spend:" in out
    assert "$3.65" in out
