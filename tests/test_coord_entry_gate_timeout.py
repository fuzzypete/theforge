"""A reuse gate that did not finish must reach the dev agent as such (#2796).

Sprint resume runs a gate on an existing worktree and routes the story to DEV
when it does not pass. A gate killed at its time budget and a gate whose tests
failed are different conditions asking for different work, and the distinction
used to stop at the triage reason string: DEV received the ordinary
first-iteration prompt and went looking for a failing test that could not exist.

These tests cover the whole carry — the two resume callers that dispatch DEV,
the coordinator state the outcome is seeded into, the prompt the dev phase
builds from it, and the audit that records the real budget and elapsed time.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.run import cmd_run
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.dev_phase import _run_dev_phase
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    EntryGateOutcome,
    Phase,
)
from theforge.runners import AgentResult
from theforge.sprint.dag import StoryTriage
from theforge.task import TaskStory
from theforge.task.dev_prompts import build_dev_prompt

STORY = "# story\n\nMake the thing work.\n"

TIMEOUT_OUTCOME = EntryGateOutcome(
    outcome="timeout",
    command="make gate",
    timeout_s=360,
    elapsed_s=361.4,
    output_tail="collected 4103 items",
    profile="complete (merge authority)",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


def _make_config(project_root: Path, slug: str) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=project_root,
        workspace=WorkspaceConfig(
            create_command="git worktree add -b feat/{slug} {slug} {base_branch}",
            path_pattern="{slug}",
            branch_pattern=f"feat/{slug}",
            base_branch="main",
            setup_command=None,
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _run_dev_once(tmp_path: Path, state: CoordinatorState) -> str:
    """Drive one real _run_dev_phase over a git repo; return the prompt it built."""
    # Idempotent: a test that drives two consecutive iterations calls this twice
    # against the same tree.
    if not (tmp_path / ".git").exists():
        _git(tmp_path, "init", "--initial-branch=main")
        _git(tmp_path, "config", "user.email", "test@example.com")
        _git(tmp_path, "config", "user.name", "Test")
        (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "initial")
        _git(tmp_path, "checkout", "-q", "-b", "feat/t")

    failed = AgentResult(
        success=False,
        output="",
        session_id=None,
        cost_usd=0.0,
        exit_code=-2,
        raw={},
        profile_name="dev",
    )
    with (
        patch("theforge.coordinator.dev_phase.run_agent", return_value=failed) as mock_agent,
        patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
    ):
        _run_dev_phase(
            state,
            _make_config(tmp_path, "t"),
            TaskStory(name="t", slug="t"),
            STORY,
            tmp_path,
            "feat/t",
            notify=False,
            logger=None,
        )
    return str(mock_agent.call_args.kwargs["prompt"])


class TestTheTimeoutReachesTheDevPrompt:
    def test_prompt_names_the_timeout_the_budget_and_the_elapsed_time(
        self, tmp_path: Path
    ) -> None:
        """The quantities any useful response depends on are all stated."""
        state = CoordinatorState()
        state.entry_gate_outcome = TIMEOUT_OUTCOME

        prompt = _run_dev_once(tmp_path, state)

        assert "did not finish" in prompt
        assert "360s" in prompt  # the configured budget
        assert "361.4s" in prompt  # measured elapsed time
        assert "make gate" in prompt

    def test_prompt_does_not_frame_the_timeout_as_a_failing_test(self, tmp_path: Path) -> None:
        """The defect: an agent told 'the gate failed' hunts for a broken assertion."""
        state = CoordinatorState()
        state.entry_gate_outcome = TIMEOUT_OUTCOME

        prompt = _run_dev_once(tmp_path, state)

        assert "**No test failed.**" in prompt
        assert "no failing test to find" in prompt

    def test_the_outcome_is_surfaced_exactly_once(self, tmp_path: Path) -> None:
        """A later iteration is looking at its own work, not at the entry gate."""
        state = CoordinatorState()
        state.entry_gate_outcome = TIMEOUT_OUTCOME

        first = _run_dev_once(tmp_path, state)
        assert "The Gate That Sent You Here Did Not Finish" in first
        assert state.entry_gate_surfaced_to_dev is True

        second = _run_dev_once(tmp_path, state)
        assert "The Gate That Sent You Here Did Not Finish" not in second

    def test_an_ordinary_first_iteration_says_nothing_about_a_gate(self, tmp_path: Path) -> None:
        prompt = _run_dev_once(tmp_path, CoordinatorState())

        assert "The Gate That Sent You Here Did Not Finish" not in prompt
        assert CoordinatorState().entry_gate_surfaced_to_dev is False

    def test_the_prompt_section_renders_from_a_note(self, tmp_path: Path) -> None:
        prompt = build_dev_prompt(
            TaskStory(name="t", slug="t"),
            workspace_path=tmp_path,
            branch_name="feat/t",
            story_content=STORY,
            gate_command="make gate",
            entry_gate_note="the gate ran out of time",
        )
        assert "## ⚠ The Gate That Sent You Here Did Not Finish" in prompt
        assert "the gate ran out of time" in prompt


class TestResumeCallersForwardTheOutcome:
    """Both `forge sprint --resume` and `forge run --resume` dispatch DEV from
    the same triage; the sprint side is covered in test_sprint_resume.py."""

    def test_cli_resume_forwards_the_triage_timeout(self, tmp_path: Path) -> None:
        story = tmp_path / "story.md"
        story.write_text(STORY, encoding="utf-8")
        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project: test\n", encoding="utf-8")
        worktree = tmp_path / "t"
        worktree.mkdir()
        config = _make_config(tmp_path, "t")

        args = argparse.Namespace(
            story=str(story),
            slug=None,
            config=str(forge_yaml),
            base_branch=None,
            plan=None,
            from_phase=None,
            until=None,
            reviewers=None,
            max_cycles=None,
            resume=True,
            dev_model=None,
            plan_model=None,
            dry_run=False,
            interactive=False,
            auto_merge=False,
            verbose=False,
            no_notify=True,
            fg=True,
            no_pull=False,
        )
        triage = StoryTriage(
            story_path=str(story),
            action="dev",
            reason="worktree exists, gate fails (Gate timed out after 360s)",
            worktree_path=worktree,
            slug="t",
            gate_outcome=TIMEOUT_OUTCOME,
        )
        result = CoordinatorResult(
            success=True, phase=Phase.DONE, state=CoordinatorState(), message="ok"
        )

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run._triage_spec", return_value=triage),
            patch(
                "theforge.coordinator.engine.run_from_dev", return_value=result
            ) as mock_from_dev,
            patch("theforge.cli.run._write_audit", return_value=tmp_path / "audit.json"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        assert mock_from_dev.call_args.kwargs["entry_gate_outcome"] is TIMEOUT_OUTCOME


class TestTheOutcomeReachesTheAudit:
    def _audit(self, tmp_path: Path, state: CoordinatorState) -> dict:
        config = _make_config(tmp_path, "t")
        task = TaskStory(name="t", slug="t")
        result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")
        return generate_audit_log(config, task, result)

    def test_budget_and_elapsed_are_recorded_not_reconstructed(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.entry_gate_outcome = TIMEOUT_OUTCOME
        state.entry_gate_surfaced_to_dev = True

        workspace = self._audit(tmp_path, state)["workspace"]

        assert workspace["entry_gate"]["outcome"] == "timeout"
        assert workspace["entry_gate"]["timeout_s"] == 360
        assert workspace["entry_gate"]["elapsed_s"] == 361.4
        assert workspace["entry_gate"]["command"] == "make gate"
        assert workspace["entry_gate_surfaced_to_dev"] is True

    def test_a_run_no_entry_gate_decided_records_nothing(self, tmp_path: Path) -> None:
        workspace = self._audit(tmp_path, CoordinatorState())["workspace"]

        assert workspace["entry_gate"] is None
        assert workspace["entry_gate_surfaced_to_dev"] is False
