"""Tests for coordinator state helpers.

Covers: StructuredLogger, LogConfig, _get_commit_log, audit fields.
"""

import json as _json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_pool_config,
    _make_task,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
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
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.logging import StructuredLogger
from theforge.coordinator.review_context import _get_commit_log
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.coordinator.util import _fmt_duration, _generate_run_id
from theforge.runners import AgentResult
from theforge.story_validator import StoryValidationResult


class TestTotalCostIncludesStoryValidation:
    """Tests that total_cost includes story_validation_result.cost_usd."""

    def test_total_cost_includes_story_validation_when_present(self):
        state = CoordinatorState()
        state.story_validation_result = StoryValidationResult(
            verdict="PASS",
            findings=[],
            cost_usd=0.05,
        )
        assert state.total_cost == pytest.approx(0.05)

    def test_total_cost_excludes_story_validation_when_none(self):
        state = CoordinatorState()
        assert state.story_validation_result is None
        assert state.total_cost == pytest.approx(0.0)

    def test_total_cost_handles_story_validation_cost_usd_none(self):
        """story_validation_result present but cost_usd is None — treat as 0."""
        state = CoordinatorState()
        state.story_validation_result = StoryValidationResult(
            verdict="PASS",
            findings=[],
            cost_usd=None,
        )
        assert state.total_cost == pytest.approx(0.0)

    def test_total_cost_handles_story_validation_cost_zero(self):
        """cost_usd of exactly 0.0 should be preserved, not treated as missing."""
        state = CoordinatorState()
        state.story_validation_result = StoryValidationResult(
            verdict="PASS",
            findings=[],
            cost_usd=0.0,
        )
        assert state.total_cost == pytest.approx(0.0)


class TestStructuredLogger:
    """Unit tests for StructuredLogger class."""

    def test_emits_valid_json(self, tmp_path):
        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="abc123",
            project="proj",
            task="my-task",
            log_file=str(log_file),
            enabled=True,
        )
        logger.emit("test_event", key="value")
        lines = log_file.read_text().splitlines()
        assert len(lines) == 1
        entry = _json.loads(lines[0])
        assert entry["ts"]
        assert entry["project"] == "proj"
        assert entry["run_id"] == "abc123"
        assert entry["task"] == "my-task"
        assert entry["event"] == "test_event"
        assert entry["key"] == "value"

    def test_creates_directory(self, tmp_path):
        log_file = tmp_path / "deep" / "nested" / "forge.log"
        logger = StructuredLogger(
            run_id="r1",
            project="p",
            task="t",
            log_file=str(log_file),
            enabled=True,
        )
        logger.emit("test_event")
        assert log_file.exists()
        entry = _json.loads(log_file.read_text().splitlines()[0])
        assert entry["event"] == "test_event"

    def test_appends_not_overwrites(self, tmp_path):
        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="r1",
            project="p",
            task="t",
            log_file=str(log_file),
            enabled=True,
        )
        logger.emit("event_one")
        logger.emit("event_two")
        lines = log_file.read_text().splitlines()
        assert len(lines) == 2
        assert _json.loads(lines[0])["event"] == "event_one"
        assert _json.loads(lines[1])["event"] == "event_two"

    def test_write_failure_is_silent(self, tmp_path):
        logger = StructuredLogger(
            run_id="r1",
            project="p",
            task="t",
            log_file="/nonexistent_root_dir/impossible/forge.log",
            enabled=True,
        )
        # Must not raise
        logger.emit("test_event")

    def test_disabled_does_not_write(self, tmp_path):
        log_file = tmp_path / "forge.log"
        logger = StructuredLogger(
            run_id="r1",
            project="p",
            task="t",
            log_file=str(log_file),
            enabled=False,
        )
        logger.emit("test_event")
        assert not log_file.exists()

    def test_project_substitution(self, tmp_path):
        log_file = str(tmp_path / "{project}" / "forge.log")
        logger = StructuredLogger(
            run_id="r1",
            project="myproj",
            task="t",
            log_file=log_file,
            enabled=True,
        )
        logger.emit("test_event")
        expected = tmp_path / "myproj" / "forge.log"
        assert expected.exists()

    def test_generate_run_id_returns_hex_string(self):
        rid = _generate_run_id()
        assert len(rid) == 12
        assert all(c in "0123456789abcdef" for c in rid)


class TestLogConfigParsing:
    """Test LogConfig parsing from forge.yaml."""

    def test_log_config_parsed_from_forge_yaml(self, tmp_path):
        from theforge.config import load_config

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text(
            "project: myproj\n"
            "logging:\n"
            "  log_file: /tmp/test_forge.log\n"
            "  enabled: false\n"
            "profiles:\n"
            "  dev:\n"
            "    cli: claude\n"
            "    model: sonnet\n"
            "    budget_usd: 1.0\n"
            "    timeout_seconds: 300\n"
            "  review:\n"
            "    cli: claude\n"
            "    model: opus\n"
            "    budget_usd: 1.0\n"
            "    timeout_seconds: 300\n",
            encoding="utf-8",
        )
        config = load_config(forge_yaml)
        assert config.log.log_file == "/tmp/test_forge.log"
        assert config.log.enabled is False

    def test_log_config_defaults_when_absent(self, tmp_path):
        from theforge.config import load_config

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text(
            "project: myproj\n"
            "profiles:\n"
            "  dev:\n"
            "    cli: claude\n"
            "    model: sonnet\n"
            "    budget_usd: 1.0\n"
            "    timeout_seconds: 300\n"
            "  review:\n"
            "    cli: claude\n"
            "    model: opus\n"
            "    budget_usd: 1.0\n"
            "    timeout_seconds: 300\n",
            encoding="utf-8",
        )
        config = load_config(forge_yaml)
        assert config.log.log_file == ".forge/logs/forge.log"
        assert config.log.enabled is True


class TestStructuredLoggingIntegration:
    """Integration tests: logging events emitted during run_task."""

    def _make_logging_config(self, tmp_path: Path, log_file: Path) -> ForgeConfig:
        return ForgeConfig(
            project="testproj",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
            log=LogConfig(log_file=str(log_file), enabled=True),
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_task_emits_lifecycle_events(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        log_file = tmp_path / "forge.log"
        config = self._make_logging_config(tmp_path, log_file)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir(parents=True, exist_ok=True)

        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="verdict: PROCEED\ncomplexity: small",
            cost_usd=0.10,
        )
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.50)
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        assert result.success is True

        assert log_file.exists()
        lines = log_file.read_text().splitlines()
        events = [_json.loads(line)["event"] for line in lines]

        assert "run_start" in events
        assert "run_end" in events
        # Phase events
        assert "phase_start" in events
        assert "phase_end" in events
        assert "gate_result" in events
        assert "review_result" in events

        # All events share the same run_id
        run_ids = {_json.loads(line)["run_id"] for line in lines}
        assert len(run_ids) == 1

        # All events have required fields
        for line in lines:
            entry = _json.loads(line)
            assert "ts" in entry
            assert "project" in entry
            assert "run_id" in entry
            assert "task" in entry
            assert "event" in entry

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_result_includes_output_tail(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        log_file = tmp_path / "forge.log"
        config = self._make_logging_config(tmp_path, log_file)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir(parents=True, exist_ok=True)

        long_output = "x" * 1000

        def shell_with_long_output(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, long_output)
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        mock_shell.side_effect = shell_with_long_output
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="verdict: PROCEED\ncomplexity: small",
            cost_usd=0.10,
        )
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.50)
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        lines = log_file.read_text().splitlines()
        gate_events = [
            _json.loads(line) for line in lines if _json.loads(line)["event"] == "gate_result"
        ]
        assert len(gate_events) >= 1
        output_tail = gate_events[0]["output_tail"]
        assert len(output_tail) <= 500

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_result_event_fields(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        log_file = tmp_path / "forge.log"
        config = self._make_logging_config(tmp_path, log_file)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir(parents=True, exist_ok=True)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="verdict: PROCEED\ncomplexity: small",
            cost_usd=0.10,
        )
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.50)
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        lines = log_file.read_text().splitlines()
        review_events = [
            _json.loads(line) for line in lines if _json.loads(line)["event"] == "review_result"
        ]
        assert len(review_events) >= 1
        ev = review_events[0]
        assert ev["verdict"] == "APPROVE"
        assert ev["p1_count"] == 0
        assert ev["p2_count"] == 0
        assert "cost_usd" in ev

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escalate_event_emitted_on_gate_failure(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        log_file = tmp_path / "forge.log"
        config = self._make_logging_config(tmp_path, log_file)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir(parents=True, exist_ok=True)

        # Always return FAIL → exhaust retries → ESCALATE
        mock_shell.side_effect = _shell_with_gate(workspace, "FAIL")
        mock_preflight.return_value = _make_agent_result(
            success=True,
            output="verdict: PROCEED\ncomplexity: small",
            cost_usd=0.10,
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Done.", cost_usd=0.10),
            _make_agent_result(success=True, output="Done.", cost_usd=0.10),
            _make_agent_result(success=True, output="Done.", cost_usd=0.10),
        ]
        mock_pool.return_value = []

        result = run_task(config, task)
        assert result.success is False

        lines = log_file.read_text().splitlines()
        events = [_json.loads(line)["event"] for line in lines]
        assert "escalate" in events
        assert "run_end" in events

        run_end = next(
            _json.loads(line) for line in lines if _json.loads(line)["event"] == "run_end"
        )
        assert run_end["outcome"] == "escalate"

    def test_log_write_failure_does_not_crash_run(self, tmp_path):
        """A broken emit() must never crash the run."""
        log_file = tmp_path / "forge.log"
        config = self._make_logging_config(tmp_path, log_file)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir(parents=True, exist_ok=True)

        with (
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_agent,
            patch("theforge.coordinator.util._run_shell") as mock_shell,
            patch(
                "theforge.coordinator.engine.StructuredLogger.emit",
                side_effect=OSError("disk full"),
            ),
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            mock_preflight.return_value = _make_agent_result(
                success=True,
                output="verdict: PROCEED\ncomplexity: small",
                cost_usd=0.10,
            )
            mock_agent.return_value = _make_agent_result(
                success=True, output="Done.", cost_usd=0.50
            )
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            # The run must succeed despite OSError from emit
            result = run_task(config, task)
        assert result.success is True


# ── _get_commit_log ───────────────────────────────────────────────────


class TestGetCommitLog:
    """Tests for the _get_commit_log() helper."""

    @patch("theforge.coordinator.util._run_shell")
    def test_success_clean_worktree(self, mock_shell: MagicMock, tmp_path: Path) -> None:
        """Returns commit log when command succeeds and worktree is clean."""
        log = "abc1234 feat(foo): implement the thing\ndef5678 test(foo): add tests"
        mock_shell.side_effect = [(True, ""), (True, log)]  # status clean, log ok
        result = _get_commit_log(tmp_path, "main")
        assert result == log
        assert "WARNING" not in result

    @patch("theforge.coordinator.util._run_shell")
    def test_no_commits_clean(self, mock_shell: MagicMock, tmp_path: Path) -> None:
        """Returns placeholder when no commits ahead of base."""
        mock_shell.side_effect = [(True, ""), (True, "")]  # clean, no commits
        result = _get_commit_log(tmp_path, "main")
        assert result == "(no commits ahead of base branch)"

    @patch("theforge.coordinator.util._run_shell")
    def test_command_fails(self, mock_shell: MagicMock, tmp_path: Path) -> None:
        """Returns placeholder when git command fails."""
        mock_shell.side_effect = [(True, ""), (False, "")]  # clean, log fails
        result = _get_commit_log(tmp_path, "main")
        assert result == "(no commits ahead of base branch)"

    @patch("theforge.coordinator.util._run_shell")
    def test_dirty_worktree_with_commits(self, mock_shell: MagicMock, tmp_path: Path) -> None:
        """Appends warning when worktree has uncommitted changes."""
        log = "abc1234 feat(foo): implement the thing"
        mock_shell.side_effect = [(True, " M src/foo.py\n"), (True, log)]
        result = _get_commit_log(tmp_path, "main")
        assert "abc1234" in result
        assert "WARNING" in result
        assert "uncommitted" in result

    @patch("theforge.coordinator.util._run_shell")
    def test_dirty_worktree_no_commits(self, mock_shell: MagicMock, tmp_path: Path) -> None:
        """Warns about uncommitted changes even when no commits ahead."""
        mock_shell.side_effect = [(True, " M src/foo.py\n"), (True, "")]
        result = _get_commit_log(tmp_path, "main")
        assert "(no commits ahead of base branch)" in result
        assert "WARNING" in result


# ── _fmt_duration ─────────────────────────────────────────────────────


def test_fmt_duration_seconds():
    assert _fmt_duration(0) == "0s"
    assert _fmt_duration(1) == "1s"
    assert _fmt_duration(59) == "59s"
    assert _fmt_duration(59.9) == "59s"


def test_fmt_duration_minutes():
    assert _fmt_duration(60) == "1m 0s"
    assert _fmt_duration(61) == "1m 1s"
    assert _fmt_duration(90) == "1m 30s"
    assert _fmt_duration(3599) == "59m 59s"


def test_fmt_duration_hours():
    assert _fmt_duration(3600) == "1h 0m 0s"
    assert _fmt_duration(3661) == "1h 1m 1s"
    assert _fmt_duration(7384) == "2h 3m 4s"


# ── Audit review pool field tests ─────────────────────────────────────


class TestAuditReviewPoolFields:
    """Tests for generate_audit_log() review pool field serialization."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_audit_review_pool_fields_populated(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """When pool has 2 reviewers and one fails, audit has correct pool/successful/failed."""
        config = _make_pool_config(
            tmp_path,
            profiles=[
                ModelProfile(
                    name="opus",
                    cli="claude",
                    model="claude-opus-4-6",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
                ModelProfile(
                    name="codex",
                    cli="codex",
                    model="codex",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
            ],
            synthesis=ModelProfile(
                name="synthesis",
                cli="claude",
                model="claude-sonnet-4-6",
                budget_usd=5.0,
                timeout_seconds=300,
                allowed_tools=[],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        # opus succeeds, codex fails
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id=None,
                cost_usd=0.10,
                exit_code=0,
                raw={},
                profile_name="opus",
            ),
            AgentResult(
                success=False,
                output="",
                session_id=None,
                cost_usd=0.0,
                exit_code=1,
                raw={},
                profile_name="codex",
            ),
        ]

        result = run_task(config, task)

        audit = generate_audit_log(config, task, result)
        reviews = audit["reviews"]
        assert len(reviews) == 1
        cycle = reviews[0]
        assert set(cycle["pool_models"]) == {"opus", "codex"}
        assert cycle["successful"] == ["opus"]
        assert cycle["failed"] == ["codex"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_audit_failed_reviewer_detail(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Failed reviewer includes exit code in failed_detail."""
        config = _make_pool_config(
            tmp_path,
            profiles=[
                ModelProfile(
                    name="opus",
                    cli="claude",
                    model="claude-opus-4-6",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
                ModelProfile(
                    name="codex",
                    cli="codex",
                    model="codex",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
            ],
            synthesis=ModelProfile(
                name="synthesis",
                cli="claude",
                model="claude-sonnet-4-6",
                budget_usd=5.0,
                timeout_seconds=300,
                allowed_tools=[],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id=None,
                cost_usd=0.10,
                exit_code=0,
                raw={},
                profile_name="opus",
            ),
            AgentResult(
                success=False,
                output="",
                session_id=None,
                cost_usd=0.0,
                exit_code=1,
                raw={},
                profile_name="codex",
            ),
        ]

        result = run_task(config, task)

        audit = generate_audit_log(config, task, result)
        cycle = audit["reviews"][0]
        assert "failed_detail" in cycle
        assert "codex" in cycle["failed_detail"]
        assert "exit=1" in cycle["failed_detail"]["codex"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_audit_synthesized_flag_false_degraded(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """synthesized=False when degraded to single reviewer (one failed)."""
        config = _make_pool_config(
            tmp_path,
            profiles=[
                ModelProfile(
                    name="opus",
                    cli="claude",
                    model="claude-opus-4-6",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
                ModelProfile(
                    name="codex",
                    cli="codex",
                    model="codex",
                    budget_usd=5.0,
                    timeout_seconds=300,
                    allowed_tools=[],
                ),
            ],
            synthesis=ModelProfile(
                name="synthesis",
                cli="claude",
                model="claude-sonnet-4-6",
                budget_usd=5.0,
                timeout_seconds=300,
                allowed_tools=[],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        # One fails → degraded, no synthesis
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id=None,
                cost_usd=0.10,
                exit_code=0,
                raw={},
                profile_name="opus",
            ),
            AgentResult(
                success=False,
                output="",
                session_id=None,
                cost_usd=0.0,
                exit_code=1,
                raw={},
                profile_name="codex",
            ),
        ]

        result = run_task(config, task)

        audit = generate_audit_log(config, task, result)
        assert audit["reviews"][0]["synthesized"] is False


# ── Campaign audit tests ──────────────────────────────────────────────


class TestCampaignAuditWrites:
    """Tests for campaign audit writing behavior."""

    def _make_manifest(self, tmp_path: Path, spec_rel_paths: list[str]) -> Path:
        """Create a campaign manifest YAML in tmp_path."""
        manifest = {
            "name": "Test Campaign",
            "budget_usd": 100.0,
            "specs": spec_rel_paths,
        }
        manifest_path = tmp_path / "campaign.yaml"
        manifest_path.write_text(yaml.dump(manifest), encoding="utf-8")
        return manifest_path

    def _make_fake_result(self, tmp_path: Path) -> object:
        """Build a fake CoordinatorResult with one APPROVE review cycle."""
        from theforge.coordinator.state import (
            CoordinatorResult,
            CoordinatorState,
            ReviewCycleMetadata,
        )
        from theforge.review import ReviewResult

        state = CoordinatorState()
        meta = ReviewCycleMetadata(
            pool_models=["opus", "codex"],
            successful=["opus"],
            failed=["codex"],
            synthesized=False,
            failed_detail={"codex": "exit=1"},
        )
        state.review_cycle_metadata.append(meta)
        state.review_cycle = 1
        rr = ReviewResult(
            verdict="APPROVE",
            summary="Looks good.",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        state.review_results.append(rr)
        return CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done",
        )

    @patch("theforge.sprint.runner.run_task")
    def test_campaign_writes_worktree_audit(self, mock_run_task, tmp_path):
        """After run_sprint(), the spec worktree contains .forge/audit.yaml."""
        from theforge.sprint import run_sprint

        config = _make_config(tmp_path)

        # Create a spec file
        spec = tmp_path / "spec.md"
        spec.write_text("---\nslug: my-spec\nname: My Spec\n---\n# Spec", encoding="utf-8")

        # Pre-create the workspace (simulates coordinator having created it)
        workspace = tmp_path / "my-spec"
        workspace.mkdir()

        fake_result = self._make_fake_result(tmp_path)
        mock_run_task.return_value = fake_result

        manifest_path = self._make_manifest(tmp_path, ["spec.md"])
        run_sprint(config, manifest_path)

        audit_path = workspace / ".forge" / "audit.yaml"
        assert audit_path.exists(), ".forge/audit.yaml not written to worktree"
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8")) or {}
        assert "reviews" in audit
        assert len(audit["reviews"]) == 1
        assert audit["reviews"][0]["failed"] == ["codex"]
        assert audit["reviews"][0].get("failed_detail", {}).get("codex") == "exit=1"

    @patch("theforge.sprint.runner.run_task")
    def test_sprint_audit_includes_review_summary(self, mock_run_task, tmp_path):
        """sprint-audit.yaml has reviews list per spec with pool/successful/failed fields."""
        from theforge.sprint import run_sprint

        config = _make_config(tmp_path)

        spec = tmp_path / "spec.md"
        spec.write_text("---\nslug: my-spec\nname: My Spec\n---\n# Spec", encoding="utf-8")

        workspace = tmp_path / "my-spec"
        workspace.mkdir()

        fake_result = self._make_fake_result(tmp_path)
        mock_run_task.return_value = fake_result

        manifest_path = self._make_manifest(tmp_path, ["spec.md"])
        run_sprint(config, manifest_path)

        sprint_audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        assert sprint_audit_path.exists(), "sprint-audit.yaml not written"
        audit = yaml.safe_load(sprint_audit_path.read_text(encoding="utf-8")) or {}
        specs = audit.get("specs", [])
        assert len(specs) == 1
        spec_entry = specs[0]
        assert "reviews" in spec_entry
        reviews = spec_entry["reviews"]
        assert len(reviews) == 1
        cycle = reviews[0]
        assert set(cycle["pool"]) == {"opus", "codex"}
        assert cycle["successful"] == ["opus"]
        assert cycle["failed"] == ["codex"]
        assert cycle["verdict"] == "APPROVE"
        assert cycle["p1_count"] == 0
        assert cycle["p2_count"] == 0

    @patch("theforge.sprint.runner.run_task")
    def test_campaign_already_done_no_worktree_audit(self, mock_run_task, tmp_path):
        """ALREADY_DONE specs do not write a worktree audit (no worktree was created)."""
        from theforge.coordinator.state import CoordinatorResult, CoordinatorState
        from theforge.sprint import run_sprint

        config = _make_config(tmp_path)

        spec = tmp_path / "spec.md"
        spec.write_text("---\nslug: done-spec\nname: Done Spec\n---\n# Spec", encoding="utf-8")

        # No workspace created — ALREADY_DONE path
        state = CoordinatorState()
        state.preflight_verdict = "ALREADY_DONE"
        fake_result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Already done",
        )
        mock_run_task.return_value = fake_result

        manifest_path = self._make_manifest(tmp_path, ["spec.md"])
        run_sprint(config, manifest_path)

        # No workspace dir → no .forge/audit.yaml written there
        workspace = tmp_path / "done-spec"
        assert not workspace.exists() or not (workspace / ".forge" / "audit.yaml").exists()
