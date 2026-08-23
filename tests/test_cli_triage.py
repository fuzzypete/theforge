"""Tests for the ``forge triage`` command layer.

The command now has two read-only modes:

* bare ``forge triage`` generates the deterministic backlog report artifact;
* ``forge triage --report ...`` consumes an existing artifact for the advisory
  proposal stage.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from theforge.cli import triage as cli_triage
from theforge.cli.main import build_parser
from theforge.config import DEFAULT_PREFLIGHT_PROFILE
from theforge.triage_backlog_report import (
    BacklogFindingRecord,
    BacklogTriageReport,
    EvidenceEntry,
)
from theforge.triage_proposal import (
    FindingPacket,
    FindingProposalResult,
    ProposalRunSummary,
    needs_verification_proposal,
)
from theforge.triage_report import BacklogReport

_REPORT = {
    "current_milestone": "v0.12.0",
    "findings": [
        {
            "finding_id": "1312:audit-count",
            "issue_ref": "#1312",
            "body": "audit count is off by one",
            "evidence": [
                {
                    "id": "symbol-absent",
                    "kind": "staleness",
                    "summary": "cited symbol absent from current tree",
                    "checkable": True,
                }
            ],
        }
    ],
}


def _write_project(tmp_path: Path, report: dict) -> tuple[Path, Path]:
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: demo\n", encoding="utf-8")
    report_path = tmp_path / "backlog.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return config_path, report_path


def _args(**kwargs: object) -> argparse.Namespace:
    defaults = {
        "report": None,
        "ratify": None,
        "discard": None,
        "output": None,
        "config": None,
        "current_milestone": None,
        "no_audit": True,
        "verbose": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture(autouse=True)
def _operator_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default these tests to an operator at the keyboard.

    ``forge triage`` picks its mode from stdin (#2231), and a pytest process
    never has a TTY — so without this every test in the file would exercise the
    headless path. Tests that mean the headless path say so explicitly.
    """
    monkeypatch.setattr(cli_triage, "stdin_is_interactive", lambda: True)


class _FakeConfig:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.secrets: dict[str, str] = {}
        self.conventions_advisory = type(
            "_Advice",
            (),
            {"issue_filing": type("_IssueFiling", (), {"milestone": "v0.12.0"})()},
        )()


def _stub_config(monkeypatch: pytest.MonkeyPatch, project_root: Path) -> None:
    monkeypatch.setattr(
        cli_triage, "load_config_checked", lambda *a, **k: _FakeConfig(project_root)
    )


def _stub_flow(monkeypatch: pytest.MonkeyPatch, summary: ProposalRunSummary) -> list[dict]:
    calls: list[dict] = []

    def _run(report: BacklogReport, config: object, **kwargs: object) -> ProposalRunSummary:
        calls.append({"report": report, **kwargs})
        return summary

    import theforge.coordinator.triage_headless_flow as headless_flow
    import theforge.coordinator.triage_proposal_flow as flow

    monkeypatch.setattr(flow, "run_triage_proposals", _run)
    # The headless path reads the run back out of the audit substrate before it
    # will publish a pending decision. These tests stub the proposal flow, so
    # nothing was really recorded — stand in for the reload the real run would
    # get. Its absence is covered as its own case in test_triage_headless.py.
    monkeypatch.setattr(
        headless_flow,
        "_recorded_events",
        lambda root, run_id: (
            [
                {
                    "finding_id": "1312:audit-count",
                    "issue_ref": "#1312",
                    "disposition": "needs_verification",
                    "proposal": {"disposition": "needs_verification"},
                }
            ]
            * summary.findings_count
        ),
    )
    return calls


def _packet() -> FindingPacket:
    return FindingPacket(
        finding_id="1312:audit-count",
        issue_ref="#1312",
        finding_body="audit count is off by one",
    )


def _summary(**kwargs: object) -> ProposalRunSummary:
    result = FindingProposalResult(
        finding_id="1312:audit-count",
        issue_ref="#1312",
        packet_hash="abc",
        proposal=needs_verification_proposal(_packet(), basis="no checkable artifact cited"),
        cost_usd=0.0123,
        cost_provenance="provider_reported",
    )
    defaults = {
        "results": (result,),
        "total_cost_usd": 0.0123,
        "cost_provenance": "provider_reported",
    }
    defaults.update(kwargs)
    return ProposalRunSummary(**defaults)


def _backlog_report(**kwargs: object) -> BacklogTriageReport:
    finding = BacklogFindingRecord(
        finding_id="#1312",
        issue_ref="#1312",
        issue_number=1312,
        title="audit count is off by one",
        body="audit count is off by one",
        labels=("bug", "forge-finding"),
        display_labels="bug",
        opened_at="2026-05-31T00:00:00Z",
        age_days=84,
        pool_state="Hygiene",
        verification_status="stale_evidence",
        evidence=(
            EvidenceEntry(
                evidence_id="symbol-absent",
                kind="staleness",
                summary="cited symbol audit_count absent from current tree",
                observed_status="stale_evidence",
            ),
            EvidenceEntry(
                evidence_id="path-churn",
                kind="churn",
                summary="cited file src/theforge/cli/triage.py changed 3 time(s) since filing",
            ),
        ),
    )
    defaults = {
        "generated_at": "2026-08-23T00:00:00Z",
        "current_milestone": "v0.12.0",
        "named_milestones": ("v0.13.0",),
        "findings": (finding,),
        "milestone_inventory_error": None,
    }
    defaults.update(kwargs)
    return BacklogTriageReport(**defaults)


class TestParser:
    def test_triage_accepts_bare_report_generation(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["triage"])
        assert args.command == "triage"
        assert args.report is None

    def test_triage_accepts_report_mode(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["triage", "--report", "backlog.json"])
        assert args.command == "triage"
        assert args.report == "backlog.json"

    def test_triage_accepts_ratify_mode(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["triage", "--ratify", "run123"])
        assert args.command == "triage"
        assert args.ratify == "run123"

    def test_current_milestone_override_is_accepted(self) -> None:
        args = build_parser().parse_args(
            ["triage", "--report", "b.json", "--current-milestone", "v0.13.0"]
        )
        assert args.current_milestone == "v0.13.0"

    def test_output_flag_is_accepted_for_report_generation(self) -> None:
        args = build_parser().parse_args(["triage", "--output", ".forge/triage/out.yaml"])
        assert args.output == ".forge/triage/out.yaml"


class TestCommand:
    def test_report_mode_prints_the_generated_backlog_and_artifact_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, _report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        report = _backlog_report()
        seen: dict[str, object] = {}

        monkeypatch.setattr(
            cli_triage,
            "collect_backlog_report",
            lambda project_root, current_milestone=None: (
                seen.update({"project_root": project_root, "current_milestone": current_milestone})
                or report
            ),
        )

        artifact_path = tmp_path / ".forge" / "triage" / "report.yaml"
        monkeypatch.setattr(
            cli_triage,
            "write_backlog_report",
            lambda project_root, built_report, output_path=None: artifact_path,
        )

        code = cli_triage.cmd_triage(_args(config=str(config_path)))
        out = capsys.readouterr().out
        assert code == 0
        assert "FINDING BACKLOG — 1 open" in out
        assert "STALE-EVIDENCE: cited symbol audit_count absent from current tree" in out
        assert "structured report: .forge/triage/report.yaml" in out
        assert seen["project_root"] == tmp_path
        assert seen["current_milestone"] == "v0.12.0"

    def test_report_mode_uses_output_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path, _report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli_triage,
            "collect_backlog_report",
            lambda *a, **k: _backlog_report(),
        )
        seen: dict[str, object] = {}

        def _write(
            project_root: Path,
            report: BacklogTriageReport,
            output_path: Path | None = None,
        ) -> Path:
            seen["output_path"] = output_path
            return tmp_path / "custom.yaml"

        monkeypatch.setattr(cli_triage, "write_backlog_report", _write)
        assert (
            cli_triage.cmd_triage(
                _args(config=str(config_path), output=str(tmp_path / "custom.yaml"))
            )
            == 0
        )
        assert seen["output_path"] == (tmp_path / "custom.yaml").resolve()

    def test_report_mode_surfaces_collection_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, _report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli_triage,
            "collect_backlog_report",
            lambda *a, **k: (_ for _ in ()).throw(
                cli_triage.TriageBacklogReportError("gh api failed")
            ),
        )

        code = cli_triage.cmd_triage(_args(config=str(config_path)))
        assert code == 1
        assert "gh api failed" in capsys.readouterr().err

    def test_proposals_and_spend_are_printed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _stub_flow(monkeypatch, _summary())

        code = cli_triage.cmd_triage(_args(report=str(report_path), config=str(config_path)))
        out = capsys.readouterr().out
        assert code == 0
        assert "#1312  PROPOSE needs_verification" in out
        assert "cost: $0.0123" in out
        assert "TOTAL SPEND: $0.0123" in out
        assert "no issue was modified" in out

    def test_empty_backlog_prints_an_explicit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, report_path = _write_project(tmp_path, {"findings": []})
        _stub_config(monkeypatch, tmp_path)
        _stub_flow(monkeypatch, ProposalRunSummary(results=(), total_cost_usd=0.0))

        code = cli_triage.cmd_triage(_args(report=str(report_path), config=str(config_path)))
        out = capsys.readouterr().out
        assert code == 0
        assert "no findings" in out
        assert "$0.0000" in out

    def test_missing_report_fails_legibly_without_running_the_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        calls = _stub_flow(monkeypatch, _summary())

        code = cli_triage.cmd_triage(
            _args(report=str(tmp_path / "absent.json"), config=str(config_path))
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "not found" in err
        assert calls == []

    def test_missing_config_fails_before_anything_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(cli_triage, "_find_config", lambda *a, **k: None)
        code = cli_triage.cmd_triage(_args())
        assert code == 1
        assert "forge.yaml not found" in capsys.readouterr().err

    def test_current_milestone_flag_reaches_the_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        calls = _stub_flow(monkeypatch, _summary())

        cli_triage.cmd_triage(
            _args(
                report=str(report_path),
                config=str(config_path),
                current_milestone="v0.14.0",
            )
        )
        assert calls[0]["current_milestone"] == "v0.14.0"

    def test_end_to_end_through_the_real_flow_with_a_stubbed_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The command → flow → schema → substrate seam, with only the agent stubbed.

        The other command tests replace the flow, so this is the one that would
        catch the argument names, the report handoff, or the render diverging
        from what the flow actually returns.
        """
        import theforge.coordinator.triage_proposal_flow as flow
        from theforge.coordinator import audit_read_model, audit_storage

        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)

        class _Result:
            def __init__(self, output: str, cost_usd: float) -> None:
                self.output = output
                self.success = True
                self.cost_usd = cost_usd
                self.cost_provenance = "provider_reported"

        calls = {"count": 0}

        def _run_agent(**kwargs: object) -> _Result:
            calls["count"] += 1
            if calls["count"] == 1:
                return _Result(
                    "<triage_proposal>\n"
                    "disposition: punt\n"
                    "punt_reason_code: verified-stale\n"
                    "evidence:\n"
                    "  - ref: symbol-absent\n"
                    "    quote: cited symbol absent from current tree\n"
                    "</triage_proposal>",
                    0.05,
                )
            return _Result(
                "<triage_punt_review>\n"
                "verdict: concur\n"
                "evidence:\n"
                "  - ref: symbol-absent\n"
                "    quote: cited symbol absent from current tree\n"
                "</triage_punt_review>",
                0.02,
            )

        monkeypatch.setattr(flow, "run_agent", _run_agent)
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)
        monkeypatch.setattr(
            flow, "_select_advisor_profile", lambda config: DEFAULT_PREFLIGHT_PROFILE
        )

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=False)
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "#1312  PROPOSE punt (reason: verified-stale)" in out
        assert "REVIEW: concur" in out
        assert "review cost: $0.0200" in out
        assert "TOTAL SPEND: $0.0700" in out

        conn = audit_storage.open_readonly(tmp_path)
        try:
            events = list(audit_read_model.iter_triage_proposal_events(conn))
            runs = audit_read_model.triage_proposal_run_spend(conn)
        finally:
            conn.close()
        assert events[0]["proposal"]["punt_reason_code"] == "verified-stale"
        assert events[0]["punt_review"]["verdict"] == "concur"
        assert runs[0]["total_cost_usd"] == pytest.approx(0.07)
        assert runs[0]["review_stage"]["reviewed_punt_count"] == 1

    def test_the_command_spawns_no_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _stub_flow(monkeypatch, _summary())

        def _forbidden(*a: object, **k: object) -> None:
            raise AssertionError(f"forge triage spawned a subprocess: {a!r}")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        monkeypatch.setattr(subprocess, "check_output", _forbidden)

        assert cli_triage.cmd_triage(_args(report=str(report_path), config=str(config_path))) == 0

    def test_ratify_dispatches_to_the_ratification_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge.triage_ratification import RatificationFindingOutcome, RatificationSummary

        config_path, _report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)

        def _ratify(run_id: str, config: object, **kwargs: object) -> RatificationSummary:
            assert run_id == "run123"
            assert kwargs["project_root"] == tmp_path
            return RatificationSummary(
                triage_run_id=run_id,
                findings=(
                    RatificationFindingOutcome(
                        finding_id="1312:audit-count",
                        issue_ref="#1312",
                        decision="accept",
                        status="applied",
                        disposition="needs_verification",
                        summary="Recorded operator decision only; no tracker mutation.",
                    ),
                ),
            )

        import theforge.coordinator.triage_ratification_flow as ratify_flow

        monkeypatch.setattr(ratify_flow, "ratify_triage_run", _ratify)

        code = cli_triage.cmd_triage(
            _args(ratify="run123", config=str(config_path), no_audit=False)
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "TRIAGE RATIFICATION — run run123" in out
        assert "#1312  APPLIED (accept needs_verification)" in out

    def test_ratify_rejects_incompatible_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        code = cli_triage.cmd_triage(
            _args(
                ratify="run123",
                report=str(report_path),
                config=str(config_path),
                no_audit=False,
            )
        )
        assert code == 1
        assert "--ratify cannot be combined with --report" in capsys.readouterr().err


class TestHeadlessCommand:
    """``forge triage`` with nobody at the keyboard (#2231)."""

    @pytest.fixture(autouse=True)
    def _no_operator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_triage, "stdin_is_interactive", lambda: False)

    def test_report_mode_persists_instead_of_only_printing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge import pending as _pending

        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _stub_flow(monkeypatch, _summary(triage_run_id="run123"))

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=False)
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "pending operator decision written" in out
        entry = _pending.find_triage_pending("run123", tmp_path)
        assert entry is not None
        assert entry["findings_count"] == 1

    def test_bare_invocation_runs_the_whole_flow_and_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cron entry runs `forge triage` with no mode; it must still propose."""
        import theforge.triage_backlog_report as backlog_mod
        import theforge.triage_report as report_mod
        from theforge import pending as _pending

        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            backlog_mod, "collect_backlog_report", lambda root, current_milestone=None: object()
        )
        monkeypatch.setattr(
            backlog_mod,
            "write_backlog_report",
            lambda root, report, output_path=None: tmp_path / "backlog.yaml",
        )
        monkeypatch.setattr(
            report_mod,
            "load_backlog_report",
            lambda path: BacklogReport(findings=(), source_path=str(path)),
        )
        calls = _stub_flow(monkeypatch, _summary(triage_run_id="run123"))

        code = cli_triage.cmd_triage(_args(config=str(config_path), no_audit=False))
        assert code == 0
        assert len(calls) == 1
        assert _pending.find_triage_pending("run123", tmp_path) is not None

    def test_no_audit_is_refused_before_spending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        calls = _stub_flow(monkeypatch, _summary())

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=True)
        )
        assert code == 1
        assert "--no-audit cannot be used without an operator present" in capsys.readouterr().err
        assert calls == []

    def test_supersession_names_the_pending_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge import pending as _pending

        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _pending.write_triage_pending("earlier", {"findings_count": 4}, project_root=tmp_path)
        calls = _stub_flow(monkeypatch, _summary())

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=False)
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "triage-earlier" in err
        assert calls == []

    def test_an_unratifiable_run_fails_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge import pending as _pending

        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _stub_flow(monkeypatch, _summary(triage_run_id="run123", audit_error="disk full"))

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=False)
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "disk full" in err
        assert _pending.find_triage_pending("run123", tmp_path) is None

    def test_pre_dispatch_auth_failure_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import theforge.coordinator.triage_headless_flow as headless_flow

        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            headless_flow,
            "run_headless_triage",
            lambda *a, **k: headless_flow.HeadlessTriageOutcome(
                status=headless_flow.HEADLESS_FAILED,
                message="triage aborted agent dispatch before any proposer ran",
                error="triage aborted agent dispatch before any proposer ran",
                lines=("triage: triage aborted agent dispatch before any proposer ran",),
            ),
        )

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=False)
        )
        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "triage aborted agent dispatch before any proposer ran" in captured.err

    def test_ratify_is_refused_without_a_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import theforge.coordinator.triage_ratification_flow as ratify_flow

        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            ratify_flow,
            "ratify_triage_run",
            lambda *a, **k: pytest.fail("headless ratify must refuse before prompting"),
        )

        code = cli_triage.cmd_triage(
            _args(ratify="run123", config=str(config_path), no_audit=False)
        )
        assert code == 1
        assert "requires an interactive terminal" in capsys.readouterr().err


class TestRatifyResolvesPending:
    def test_pending_id_is_accepted_and_removed_when_every_outcome_is_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge import pending as _pending
        from theforge.triage_ratification import RatificationFindingOutcome, RatificationSummary

        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _pending.write_triage_pending("run123", {"findings_count": 1}, project_root=tmp_path)

        seen: dict[str, object] = {}

        def _ratify(run_id: str, config: object, **kwargs: object) -> RatificationSummary:
            seen["run_id"] = run_id
            return RatificationSummary(
                triage_run_id=run_id,
                findings=(
                    RatificationFindingOutcome(
                        finding_id="1312:audit-count",
                        issue_ref="#1312",
                        decision="accept",
                        status="applied",
                        disposition="needs_verification",
                    ),
                ),
            )

        import theforge.coordinator.triage_ratification_flow as ratify_flow

        monkeypatch.setattr(ratify_flow, "ratify_triage_run", _ratify)

        code = cli_triage.cmd_triage(
            _args(ratify="triage-run123", config=str(config_path), no_audit=False)
        )
        assert code == 0
        # The pending id resolved to the recorded run id the substrate holds.
        assert seen["run_id"] == "run123"
        assert "resolved and removed" in capsys.readouterr().out
        assert _pending.find_triage_pending("run123", tmp_path) is None

    def test_pending_is_kept_when_a_finding_is_not_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge import pending as _pending
        from theforge.triage_ratification import RatificationFindingOutcome, RatificationSummary

        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _pending.write_triage_pending("run123", {"findings_count": 1}, project_root=tmp_path)

        import theforge.coordinator.triage_ratification_flow as ratify_flow

        monkeypatch.setattr(
            ratify_flow,
            "ratify_triage_run",
            lambda run_id, config, **k: RatificationSummary(
                triage_run_id=run_id,
                findings=(
                    RatificationFindingOutcome(
                        finding_id="1312:audit-count",
                        issue_ref="#1312",
                        decision="accept",
                        status="failed",
                        disposition="punt",
                    ),
                ),
            ),
        )

        code = cli_triage.cmd_triage(
            _args(ratify="run123", config=str(config_path), no_audit=False)
        )
        assert code == 0
        assert "kept" in capsys.readouterr().out
        assert _pending.find_triage_pending("run123", tmp_path) is not None

    def test_discard_removes_the_record_without_applying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge import pending as _pending

        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        _pending.write_triage_pending("run123", {"findings_count": 7}, project_root=tmp_path)

        import theforge.coordinator.triage_ratification_flow as ratify_flow

        monkeypatch.setattr(
            ratify_flow,
            "ratify_triage_run",
            lambda *a, **k: pytest.fail("--discard must not ratify"),
        )

        code = cli_triage.cmd_triage(
            _args(discard="triage-run123", config=str(config_path), no_audit=False)
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "no disposition was applied" in out
        assert _pending.find_triage_pending("run123", tmp_path) is None

    def test_discard_of_an_unknown_id_fails_legibly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        code = cli_triage.cmd_triage(
            _args(discard="nope", config=str(config_path), no_audit=False)
        )
        assert code == 1
        assert "no pending triage decision matches" in capsys.readouterr().err

    def test_parser_accepts_discard_mode(self) -> None:
        args = build_parser().parse_args(["triage", "--discard", "triage-run123"])
        assert args.discard == "triage-run123"
