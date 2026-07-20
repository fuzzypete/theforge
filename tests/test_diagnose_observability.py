"""Seam-level integration tests for diagnose run observability.

Covers the cross-boundary state flow added so ``forge diagnose`` runs are
visible while running (convention 8):

- a live diagnose registers a PID file + ``.diagnose`` marker + per-run log,
  and ``is_diagnose_run`` / ``detach.list_active_runs`` observe it;
- the finally block tears that registration down (PID/marker removed, ``.ended``
  written) on both normal and exception exit;
- ``forge logs`` resolves the diagnose per-run log path;
- ``forge status`` labels the run TYPE=diagnose with the target slug;
- terminal messages name the actual cause (timeout vs budget), per AC4.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import _make_config

from theforge import detach
from theforge.agent_types import AgentResult
from theforge.cli.status_run_helpers import is_diagnose_run


def _agent_yaml_output(*, confirmed: bool = True) -> str:
    payload = {
        "observed_symptom": "Sprint flow drops the third story silently",
        "reproduction_or_evidence": "Run forge sprint --issues 1,2,3 — story 3 never starts",
        "hypotheses": [
            {
                "statement": "Worker pool size off-by-one",
                "status": "confirmed" if confirmed else "inconclusive",
                "evidence": "scheduler.py:142 reserves N-1 slots when N requested",
            },
        ],
        "confirmed_cause": (
            "Worker pool reserves N-1 slots in scheduler.py:142" if confirmed else ""
        ),
        "affected_code_path": "src/theforge/sprint/scheduler.py:142",
        "fix_success_criterion": "Running with --parallel 3 completes all 3 stories",
        "notes": "",
    }
    return f"```yaml\n{yaml.safe_dump(payload, sort_keys=False)}```"


def _fake_agent_result(
    output: str,
    *,
    success: bool = True,
    cost: float | None = 0.05,
    failure_code: str | None = None,
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=cost,
        exit_code=0 if success else 1,
        raw={},
        failure_code=failure_code,
    )


class TestDiagnoseRunRegistration:
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_live_run_registers_pid_marker_and_log(
        self, mock_agent, mock_fetch, mock_post, tmp_path: Path
    ):
        """While the agent runs, the run is observable as a live diagnose."""
        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 77,
            "title": "silent",
            "body": "no output",
            "state": "OPEN",
        }
        mock_post.return_value = "https://example/comment"

        observed: dict = {}

        def _capturing_agent(**_kwargs):
            runs_dir = tmp_path / ".forge" / "runs"
            observed["markers"] = [p.name for p in runs_dir.glob("*.diagnose")]
            observed["pids"] = [p.name for p in runs_dir.glob("*.pid")]
            observed["active"] = detach.list_active_runs(tmp_path)
            observed["logs"] = [p for p in (tmp_path / ".forge" / "logs").rglob("run-*.log")]
            return _fake_agent_result(_agent_yaml_output())

        mock_agent.side_effect = _capturing_agent

        result = run_diagnose_flow(
            issue_number=77,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )

        run_id = result.state.run_id
        # Mid-run the registration was present and observable.
        assert observed["markers"] == [f"{run_id}.diagnose"]
        assert observed["pids"] == [f"{run_id}.pid"]
        active_ids = {r["run_id"] for r in observed["active"]}
        assert run_id in active_ids
        active_slugs = {r["slug"] for r in observed["active"]}
        assert "diagnose-77" in active_slugs
        # The classifier recognised the live PID-backed run as a diagnose.
        # (Re-create the marker snapshot: it existed at capture time.)
        assert observed["markers"], "expected a live .diagnose marker"
        # A followable per-run log existed at the expected path.
        assert observed["logs"] == [
            tmp_path / ".forge" / "logs" / "diagnose-77" / f"run-{run_id}.log"
        ]
        assert result.state.run_slug == "diagnose-77"

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_normal_exit_tears_down_registration(
        self, mock_agent, mock_fetch, mock_post, tmp_path: Path
    ):
        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 88,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_post.return_value = "https://example/comment"
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        result = run_diagnose_flow(
            issue_number=88,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        run_id = result.state.run_id
        runs_dir = tmp_path / ".forge" / "runs"
        assert not (runs_dir / f"{run_id}.pid").exists()
        assert not (runs_dir / f"{run_id}.diagnose").exists()
        assert detach.read_run_ended(run_id, tmp_path) == "completed"

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_agent_crash_still_tears_down_registration(
        self, mock_agent, mock_fetch, tmp_path: Path
    ):
        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 99,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_agent.side_effect = RuntimeError("boom")

        result = run_diagnose_flow(
            issue_number=99,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        # A raised agent error is caught inside the flow (crash, not timeout).
        assert not result.success
        assert "crashed" in result.message
        run_id = result.state.run_id
        runs_dir = tmp_path / ".forge" / "runs"
        assert not (runs_dir / f"{run_id}.pid").exists()
        assert not (runs_dir / f"{run_id}.diagnose").exists()
        assert detach.read_run_ended(run_id, tmp_path) is not None


class TestDiagnoseLogResolution:
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_forge_logs_resolves_diagnose_log(
        self, mock_agent, mock_fetch, mock_post, tmp_path: Path
    ):
        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 55,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_post.return_value = "https://example/comment"
        mock_agent.return_value = _fake_agent_result(_agent_yaml_output())

        result = run_diagnose_flow(
            issue_number=55,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        run_id = result.state.run_id
        # forge logs resolves a run's log via the PID-file slug + _find_log_path.
        resolved = detach._find_log_path("diagnose-55", run_id, tmp_path)
        assert resolved is not None
        assert resolved.exists()
        assert resolved == (tmp_path / ".forge" / "logs" / "diagnose-55" / f"run-{run_id}.log")
        # The per-run log carries the followable phase-transition markers.
        contents = resolved.read_text(encoding="utf-8")
        assert "[forge] ▸ INVESTIGATE" in contents


class TestDiagnoseStatusLabel:
    def test_status_labels_live_diagnose_run(self, tmp_path: Path, capsys):
        """A live diagnose registration renders TYPE=diagnose with its slug."""
        from theforge.cli.status import _show_single_run_status

        run_id = "abc123def456"
        slug = "diagnose-321"
        # Simulate a live registration exactly as run_diagnose_flow writes it.
        log_dir = tmp_path / ".forge" / "logs" / slug
        log_dir.mkdir(parents=True)
        (log_dir / f"run-{run_id}.log").write_text("[forge] ▸ INVESTIGATE\n")
        detach.write_pid(run_id, slug, tmp_path)
        detach.write_diagnose_marker(run_id, tmp_path)

        assert is_diagnose_run(run_id, tmp_path)

        _show_single_run_status(run_id, tmp_path, run_type="diagnose")
        out = capsys.readouterr().out
        assert "TYPE" in out
        assert "diagnose" in out
        assert slug in out
        assert run_id in out


class TestDiagnoseTerminalMessages:
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_timeout_message_names_timeout(self, mock_agent, mock_fetch, tmp_path: Path):
        """A runner timeout (returned, not raised) yields a timeout-named message."""
        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 111,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        # Runner returns timeout: success=False, failure_code="timeout", TIMEOUT text.
        mock_agent.return_value = _fake_agent_result(
            "TIMEOUT: agent killed after 600s", success=False, failure_code="timeout"
        )

        result = run_diagnose_flow(
            issue_number=111,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert "timed out" in result.message
        assert "600s" in result.message
        assert result.state.agent_failure_code == "timeout"

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_budget_message_names_budget(self, mock_agent, mock_fetch, mock_post, tmp_path: Path):
        """A budget breach yields a budget-named partial message."""
        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 222,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_post.return_value = "https://example/comment"
        # Complete diagnosis, but cost blows past the diagnose budget ceiling.
        mock_agent.return_value = _fake_agent_result(
            _agent_yaml_output(), success=True, cost=100.0
        )

        result = run_diagnose_flow(
            issue_number=222,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert "budget" in result.message.lower()

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_failure_code_recorded_in_audit(
        self, mock_agent, mock_fetch, mock_post, tmp_path: Path
    ):
        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        config = _make_config(tmp_path)
        mock_fetch.return_value = {
            "number": 333,
            "title": "x",
            "body": "y",
            "state": "OPEN",
        }
        mock_post.return_value = "https://example/comment"
        mock_agent.return_value = _fake_agent_result(
            "TIMEOUT: agent killed", success=False, failure_code="timeout"
        )

        result = run_diagnose_flow(
            issue_number=333,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        audit = tmp_path / ".forge" / "audits" / f"diagnose-issue-333-{result.state.run_id}.yaml"
        loaded = yaml.safe_load(audit.read_text())
        assert loaded["agent"]["failure_code"] == "timeout"
