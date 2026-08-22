"""Tests for the ``forge triage`` command layer.

The command is a thin wrapper: resolve config, load the report, run the
proposal flow, render. What is pinned here is what the operator sees (one
proposal per finding, per-finding and total spend, the explicit empty-backlog
zero) and what the command must never do (touch an issue).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from theforge.cli import triage as cli_triage
from theforge.cli.main import build_parser
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
                {"id": "symbol-absent", "kind": "staleness", "summary": "gone", "checkable": True}
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
        "report": "",
        "config": None,
        "current_milestone": None,
        "no_audit": True,
        "verbose": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class _FakeConfig:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.secrets: dict[str, str] = {}


def _stub_config(monkeypatch: pytest.MonkeyPatch, project_root: Path) -> None:
    monkeypatch.setattr(
        cli_triage, "load_config_checked", lambda *a, **k: _FakeConfig(project_root)
    )


def _stub_flow(monkeypatch: pytest.MonkeyPatch, summary: ProposalRunSummary) -> list[dict]:
    calls: list[dict] = []

    def _run(report: BacklogReport, config: object, **kwargs: object) -> ProposalRunSummary:
        calls.append({"report": report, **kwargs})
        return summary

    import theforge.coordinator.triage_proposal_flow as flow

    monkeypatch.setattr(flow, "run_triage_proposals", _run)
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
        proposal=needs_verification_proposal(_packet(), evidence="no checkable artifact cited"),
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


class TestParser:
    def test_triage_is_registered_with_a_required_report(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["triage", "--report", "backlog.json"])
        assert args.command == "triage"
        assert args.report == "backlog.json"

    def test_report_flag_is_required(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["triage"])

    def test_current_milestone_override_is_accepted(self) -> None:
        args = build_parser().parse_args(
            ["triage", "--report", "b.json", "--current-milestone", "v0.13.0"]
        )
        assert args.current_milestone == "v0.13.0"


class TestCommand:
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
        code = cli_triage.cmd_triage(_args(report=str(tmp_path / "b.json")))
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
            output = (
                "<triage_proposal>\n"
                "disposition: punt\n"
                "punt_reason_code: verified-stale\n"
                "evidence: report shows cited symbol absent from current tree\n"
                "evidence_refs: [symbol-absent]\n"
                "</triage_proposal>"
            )
            success = True
            cost_usd = 0.05
            cost_provenance = "provider_reported"

        monkeypatch.setattr(flow, "run_agent", lambda **k: _Result())
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)
        monkeypatch.setattr(flow, "_select_advisor_profile", lambda config: object())

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=False)
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "#1312  PROPOSE punt (reason: verified-stale)" in out
        assert "TOTAL SPEND: $0.0500" in out

        conn = audit_storage.open_readonly(tmp_path)
        try:
            events = list(audit_read_model.iter_triage_proposal_events(conn))
            runs = audit_read_model.triage_proposal_run_spend(conn)
        finally:
            conn.close()
        assert events[0]["proposal"]["punt_reason_code"] == "verified-stale"
        assert runs[0]["total_cost_usd"] == pytest.approx(0.05)

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
