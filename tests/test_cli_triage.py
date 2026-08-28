"""Tests for the ``forge triage`` command layer."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from theforge.cli import triage as cli_triage
from theforge.cli.main import build_parser
from theforge.triage_backlog_report import (
    BacklogFindingRecord,
    BacklogTriageReport,
    EvidenceEntry,
)
from theforge.triage_shelved import (
    TRIAGE_PROPOSALS_SHELVED_EXIT_CODE,
    triage_proposals_shelved_message,
)

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


_SHELVED_MESSAGE = triage_proposals_shelved_message()


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

    def test_triage_help_reports_proposals_as_shelved(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["triage", "--help"])
        assert excinfo.value.code == 0
        help_text = capsys.readouterr().out
        normalized = " ".join(help_text.split())
        assert "shelved by ADR-0010" in normalized
        assert "consuming reports for disposition proposals is shelved by ADR-0010" in normalized
        assert (
            "proposal stage that would consume this override is shelved by ADR-0010" in normalized
        )


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

    def test_report_mode_is_rejected_with_adr_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli_triage,
            "_cmd_triage_proposals",
            lambda *a, **k: pytest.fail("shelved proposal mode must reject before dispatch"),
        )

        code = cli_triage.cmd_triage(_args(report=str(report_path), config=str(config_path)))
        captured = capsys.readouterr()
        assert code == TRIAGE_PROPOSALS_SHELVED_EXIT_CODE
        assert captured.out == ""
        assert captured.err == _SHELVED_MESSAGE + "\n"

    def test_report_mode_rejects_before_validating_the_report_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli_triage,
            "_cmd_triage_proposals",
            lambda *a, **k: pytest.fail("shelved proposal mode must reject before report loading"),
        )

        code = cli_triage.cmd_triage(
            _args(report=str(tmp_path / "absent.json"), config=str(config_path))
        )
        assert code == TRIAGE_PROPOSALS_SHELVED_EXIT_CODE
        assert capsys.readouterr().err == _SHELVED_MESSAGE + "\n"

    def test_missing_config_fails_before_anything_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(cli_triage, "_find_config", lambda *a, **k: None)
        code = cli_triage.cmd_triage(_args())
        assert code == 1
        assert "forge.yaml not found" in capsys.readouterr().err

    def test_current_milestone_flag_does_not_escape_the_shelf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli_triage,
            "_cmd_triage_proposals",
            lambda *a, **k: pytest.fail("shelved proposal mode must reject before dispatch"),
        )

        code = cli_triage.cmd_triage(
            _args(
                report=str(report_path),
                config=str(config_path),
                current_milestone="v0.14.0",
            )
        )
        assert code == TRIAGE_PROPOSALS_SHELVED_EXIT_CODE
        assert capsys.readouterr().err == _SHELVED_MESSAGE + "\n"

    def test_the_command_spawns_no_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)

        def _forbidden(*a: object, **k: object) -> None:
            raise AssertionError(f"forge triage spawned a subprocess: {a!r}")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        monkeypatch.setattr(subprocess, "check_output", _forbidden)

        assert (
            cli_triage.cmd_triage(_args(report=str(report_path), config=str(config_path)))
            == TRIAGE_PROPOSALS_SHELVED_EXIT_CODE
        )

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

    def test_report_mode_is_rejected_with_the_same_adr_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge import pending as _pending

        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli_triage,
            "_cmd_triage_headless",
            lambda *a, **k: pytest.fail("headless proposal mode must reject before dispatch"),
        )

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=False)
        )
        captured = capsys.readouterr()
        assert code == TRIAGE_PROPOSALS_SHELVED_EXIT_CODE
        assert captured.out == ""
        assert captured.err == _SHELVED_MESSAGE + "\n"
        assert _pending.unresolved_triage_pending(tmp_path) is None

    def test_bare_invocation_is_rejected_before_collecting_a_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import theforge.triage_backlog_report as backlog_mod

        config_path, _ = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            backlog_mod,
            "collect_backlog_report",
            lambda *a, **k: pytest.fail("headless shelf must reject before report collection"),
        )
        code = cli_triage.cmd_triage(_args(config=str(config_path), no_audit=False))
        assert code == TRIAGE_PROPOSALS_SHELVED_EXIT_CODE
        assert capsys.readouterr().err == _SHELVED_MESSAGE + "\n"

    def test_no_audit_does_not_change_the_headless_shelved_rejection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        config_path, report_path = _write_project(tmp_path, _REPORT)
        _stub_config(monkeypatch, tmp_path)

        code = cli_triage.cmd_triage(
            _args(report=str(report_path), config=str(config_path), no_audit=True)
        )
        assert code == TRIAGE_PROPOSALS_SHELVED_EXIT_CODE
        assert capsys.readouterr().err == _SHELVED_MESSAGE + "\n"

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
