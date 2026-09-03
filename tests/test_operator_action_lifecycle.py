"""Operator-action lifecycle: no auto-close on PR merge, no forge diagnose.

Slice 2 of epic #1469. Once the ``operator-action`` type is recognized at
intake (#1473), two adjacent surfaces must stop treating these issues as
dev-runnable:

- ``coordinator.completion._create_pr`` must reference an operator-action issue
  with ``Refs #N`` rather than ``Closes #N`` so a merged PR does not auto-close
  the tracked work item before the operator performs the deliverable.
- ``coordinator.diagnose_flow.run_diagnose_flow`` must refuse to diagnose an
  operator-action issue with an operator-observable message and a non-zero exit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coord_test_helpers import _make_config

from theforge.diagnose_types import DiagnosePhase
from theforge.review import ReviewResult
from theforge.sprint.shape_gate import OPERATOR_ACTION_LABEL
from theforge.task import TaskStory

# ── Helpers ────────────────────────────────────────────────────────────


def _make_merge_pr_config(tmp_path: Path):
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

    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            on_approve="merge-pr",
            auto_push=True,
            merge_strategy="squash",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_issue_task(tmp_path: Path, issue_number: int) -> TaskStory:
    # story_path=None mirrors an issue-sourced story so the PR body Story line
    # renders "(GitHub Issue #N)" and no backlog archive is attempted.
    return TaskStory(
        name="Adjacent dev work",
        story_path=None,
        slug="test-task",
        github_issue=issue_number,
    )


def _make_review_result() -> ReviewResult:
    return ReviewResult(
        verdict="APPROVE",
        summary="Adjacent work implemented.",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _sub_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _capture_pr_body(config, task) -> str:
    """Run ``_create_pr`` with mocked git/gh, return the ``--body`` argument."""
    from theforge.coordinator import completion

    review = _make_review_result()
    state = MagicMock()
    state.total_cost = 1.0
    state.total_cost_measured = 1.0
    state.dev_iteration = 1

    bodies: list[str] = []
    labels = [{"name": OPERATOR_ACTION_LABEL}] if _capture_pr_body._operator_action else []

    def _fake_run(cmd, **kwargs):
        if isinstance(cmd, list):
            if cmd[:2] == ["gh", "issue"] and "labels" in cmd:
                import json

                return _sub_result(0, stdout=json.dumps({"labels": labels}))
            if cmd[:3] == ["git", "rev-list", "--count"]:
                return _sub_result(0, stdout="1\n")
            if "pr" in cmd and "list" in cmd:
                # merged-PR lookup: no prior merged PR
                return _sub_result(0, stdout="[]")
            for i, arg in enumerate(cmd):
                if arg == "--body" and i + 1 < len(cmd):
                    bodies.append(cmd[i + 1])
        return _sub_result(0, stdout="https://github.com/x/y/pull/10")

    with patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run):
        result = completion._create_pr(config, task, "forge/test-task", review, state)
    assert result["success"] is True, result
    assert len(bodies) == 1, bodies
    return bodies[0]


_capture_pr_body._operator_action = False


# ── AC1: no auto-close on PR merge ─────────────────────────────────────


class TestNoAutoCloseForOperatorAction:
    def test_operator_action_issue_gets_refs_not_closes(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_issue_task(tmp_path, 1471)
        _capture_pr_body._operator_action = True
        try:
            body = _capture_pr_body(config, task)
        finally:
            _capture_pr_body._operator_action = False
        # Referenced (timeline cross-link) but not auto-closed on merge.
        assert "Refs #1471" in body
        assert "Closes #1471" not in body

    def test_spike_issue_gets_refs_not_closes(self, tmp_path: Path) -> None:
        """A spike closes through the recorded-outcome guard, never GitHub auto-close (#2600).

        ``Closes #N`` would hand the decision to GitHub's native default-branch
        auto-close (and to close-on-merge.yml on a release branch) before the
        guard is consulted, so a merged PR alone could close an outcomeless
        spike.
        """
        config = _make_merge_pr_config(tmp_path)
        task = TaskStory(
            name="Does the observer earn its keep?",
            story_path=None,
            slug="issue-2348",
            type="spike",
            github_issue=2348,
        )
        body = _capture_pr_body(config, task)
        assert "Refs #2348" in body
        assert "Closes #2348" not in body

    def test_dev_runnable_issue_still_gets_closes(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_issue_task(tmp_path, 1326)
        _capture_pr_body._operator_action = False
        body = _capture_pr_body(config, task)
        assert "Closes #1326" in body

    def test_lookup_failure_fails_open_to_closes(self, tmp_path: Path) -> None:
        """A gh label-lookup failure must not regress ordinary auto-close."""
        from theforge.coordinator import completion

        config = _make_merge_pr_config(tmp_path)
        task = _make_issue_task(tmp_path, 1500)
        review = _make_review_result()
        state = MagicMock()
        state.total_cost = 1.0
        state.total_cost_measured = 1.0
        state.dev_iteration = 1
        bodies: list[str] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list):
                if cmd[:2] == ["gh", "issue"] and "labels" in cmd:
                    return _sub_result(1, stderr="gh: could not resolve")
                if cmd[:3] == ["git", "rev-list", "--count"]:
                    return _sub_result(0, stdout="1\n")
                if "pr" in cmd and "list" in cmd:
                    return _sub_result(0, stdout="[]")
                for i, arg in enumerate(cmd):
                    if arg == "--body" and i + 1 < len(cmd):
                        bodies.append(cmd[i + 1])
            return _sub_result(0, stdout="https://github.com/x/y/pull/11")

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run):
            result = completion._create_pr(config, task, "forge/test-task", review, state)
        assert result["success"] is True
        assert "Closes #1500" in bodies[0]


# ── AC2 / AC3: forge diagnose refuses operator-action issues ───────────


class TestDiagnoseRefusesOperatorAction:
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_refuses_and_names_reason_without_running_agent(
        self, mock_agent, mock_fetch, tmp_path: Path
    ) -> None:
        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 1471,
            "title": "Validate v0.11 substrate",
            "body": "## Acceptance criteria\n\n- operator runs sprints",
            "state": "OPEN",
            "labels": [{"name": OPERATOR_ACTION_LABEL}],
        }

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=1471,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success is False
        assert result.state.phase == DiagnosePhase.FAILED
        # Operator-observable: names the issue number and the reason, distinct
        # from a shape-gate refusal.
        assert "1471" in result.message
        assert OPERATOR_ACTION_LABEL in result.message
        assert "diagnose" in result.message.lower()
        # No diagnosis artifact produced — the investigative agent never ran.
        mock_agent.assert_not_called()
        assert result.state.artifact is None
        # Audit still written for the refusal.
        audit_files = list((tmp_path / ".forge" / "audits").glob("diagnose-issue-1471-*.yaml"))
        assert audit_files, "expected an audit file for the refusal"

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_dev_runnable_issue_is_not_refused(
        self, mock_agent, mock_fetch, mock_post, tmp_path: Path
    ) -> None:
        """A non-operator-action issue is unaffected — diagnosis proceeds."""
        import yaml

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 42,
            "title": "broken sprint",
            "body": (
                "## Observed\n\n"
                "Story 3 never starts.\n\n"
                "## Expected\n\n"
                "This sprint run should start story 3.\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "bug"}],
        }
        payload = {
            "observed_symptom": "Sprint drops the third story",
            "reproduction_or_evidence": "Run forge sprint --issues 1,2,3",
            "hypotheses": [
                {
                    "statement": "off-by-one",
                    "status": "confirmed",
                    "evidence": "scheduler.py:1",
                    "claim_verification": {
                        "verification_type": "source",
                        "detail": "Checked against the target repository source.",
                    },
                }
            ],
            "confirmed_cause": "Worker pool reserves N-1 slots",
            "confirmed_cause_verification": {
                "verification_type": "source",
                "detail": "Checked against the target repository source.",
            },
            "affected_code_path": "src/theforge/sprint/scheduler.py",
            "fix_success_criterion": "--parallel 3 completes all 3 stories",
            "notes": "",
        }
        agent_out = f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"
        agent_result = MagicMock()
        agent_result.output = agent_out
        agent_result.success = True
        agent_result.cost_usd = 0.05
        mock_agent.return_value = agent_result
        mock_post.return_value = "https://github.com/test/repo/issues/42#issuecomment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=42,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert result.success is True
        mock_agent.assert_called_once()

    def test_cli_exit_code_is_nonzero_on_refusal(self, tmp_path: Path) -> None:
        """AC3: the CLI surfaces a non-zero exit when diagnose refuses."""
        import argparse

        from theforge.cli import diagnose as diagnose_cli

        config = _make_config(tmp_path)

        refusal = MagicMock()
        refusal.success = False
        refusal.message = (
            f"Refusing to diagnose: issue #1471 is labeled '{OPERATOR_ACTION_LABEL}'."
        )
        refusal.state.phase.name = "FAILED"
        refusal.state.agent_cost_usd = None
        refusal.state.agent_duration_s = 0.0

        args = argparse.Namespace(
            issue=["1471"],
            config=None,
            output_destination="comment",
            interactive=False,
            autonomous=True,
            parallel=None,
            dry_run=False,
            verbose=False,
        )

        with (
            patch.object(diagnose_cli, "_find_config", return_value=tmp_path / "forge.yaml"),
            patch.object(diagnose_cli, "load_config", return_value=config),
            patch("pathlib.Path.exists", return_value=True),
            patch.object(diagnose_cli, "run_diagnose_flow", return_value=refusal),
        ):
            rc = diagnose_cli.cmd_diagnose(args)
        assert rc == 1
