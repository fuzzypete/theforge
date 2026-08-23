"""Headless ``forge triage``: pending substrate, flow, status, config, post-sprint.

The CLI dispatch for the same feature lives in ``test_cli_triage.py``; this file
covers the substrate the CLI sits on and the sprint side effect that reuses it.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import yaml

from theforge import pending as _pending
from theforge.coordinator import triage_headless_flow as headless
from theforge.triage_proposal import (
    FindingPacket,
    FindingProposalResult,
    ProposalRunSummary,
    PuntReviewStage,
    needs_verification_proposal,
)


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
    defaults: dict[str, object] = {
        "results": (result,),
        "total_cost_usd": 0.0123,
        "cost_provenance": "provider_reported",
        "triage_run_id": "run123",
        "report_path": "/tmp/backlog.yaml",
        "review_stage": PuntReviewStage(
            reviewed_punt_count=1, challenged_punt_count=1, no_op=False
        ),
    }
    defaults.update(kwargs)
    return ProposalRunSummary(**defaults)  # type: ignore[arg-type]


class _FakeConfig:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.secrets: dict[str, str] = {}
        self.conventions_advisory = None


# ── Pending substrate ────────────────────────────────────────────────────────


class TestTriagePendingSubstrate:
    def test_written_record_carries_no_pid_and_no_timeout(self, tmp_path: Path) -> None:
        path = _pending.write_triage_pending(
            "run123",
            {"findings_count": 15, "pid": 4242, "timeout_at": "whenever"},
            project_root=tmp_path,
        )
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["kind"] == "triage"
        assert data["run_id"] == "triage-run123"
        assert data["triage_run_id"] == "run123"
        # The invariant: a triage record is not a live gate.
        assert "pid" not in data
        assert "timeout_at" not in data

    def test_read_pending_does_not_delete_a_pidless_triage_record(self, tmp_path: Path) -> None:
        _pending.write_triage_pending("run123", {"findings_count": 3}, project_root=tmp_path)
        first = _pending.read_pending("triage-run123", project_root=tmp_path)
        second = _pending.read_pending("triage-run123", project_root=tmp_path)
        assert first is not None
        assert second is not None
        assert (tmp_path / ".forge" / "pending" / "triage-run123.yaml").exists()

    def test_cleanup_stale_leaves_triage_but_still_sweeps_hitl(self, tmp_path: Path) -> None:
        _pending.write_triage_pending("run123", {"findings_count": 3}, project_root=tmp_path)
        _pending.write_pending(
            run_id="story-a",
            story="story-a",
            phase="HUMAN_REVIEW",
            reason="r",
            options=["approve"],
            timeout_seconds=-10,  # already expired
            project_root=tmp_path,
        )
        removed = _pending.cleanup_stale(tmp_path)
        assert removed == 1
        assert _pending.find_triage_pending("run123", tmp_path) is not None

    def test_find_accepts_either_id_and_discard_removes(self, tmp_path: Path) -> None:
        _pending.write_triage_pending("run123", {"findings_count": 3}, project_root=tmp_path)
        assert _pending.find_triage_pending("run123", tmp_path) is not None
        assert _pending.find_triage_pending("triage-run123", tmp_path) is not None
        assert _pending.find_triage_pending("nope", tmp_path) is None

        removed = _pending.discard_triage_pending("run123", tmp_path)
        assert removed is not None
        assert _pending.unresolved_triage_pending(tmp_path) is None

    def test_unresolved_returns_the_oldest(self, tmp_path: Path) -> None:
        assert _pending.unresolved_triage_pending(tmp_path) is None
        _pending.write_triage_pending("aaa", {"findings_count": 1}, project_root=tmp_path)
        entry = _pending.unresolved_triage_pending(tmp_path)
        assert entry is not None
        assert entry["triage_run_id"] == "aaa"

    def test_sprint_status_never_reads_a_triage_record_as_a_live_gate(
        self, tmp_path: Path
    ) -> None:
        from theforge.sprint.status_reader import _pending_entry_is_live

        _pending.write_triage_pending("run123", {"findings_count": 3}, project_root=tmp_path)
        entry = _pending.find_triage_pending("run123", tmp_path)
        assert entry is not None
        assert _pending_entry_is_live(entry) is False
        # Even if a future writer wrongly stamped a live pid on it.
        assert _pending_entry_is_live({**entry, "pid": 1}) is False


# ── Headless flow ────────────────────────────────────────────────────────────


class _StubReport:
    source_path = "/tmp/backlog.yaml"
    findings: tuple = ()


#: The reload a one-finding run's recording is expected to produce. Stubbed by
#: default because a run that proposed findings but reloads nothing is a
#: recording failure, not the happy path (see TestUnratifiableRuns).
_RECORDED_EVENT: dict = {
    "finding_id": "1312:audit-count",
    "issue_ref": "#1312",
    "disposition": "needs_verification",
    "evidence_refs": [],
    "proposal": {"disposition": "needs_verification"},
}


def _stub_flow(
    monkeypatch: pytest.MonkeyPatch,
    summary: ProposalRunSummary,
    *,
    events: list[dict] | None = None,
) -> list[dict]:
    calls: list[dict] = []
    import theforge.coordinator.triage_proposal_flow as flow

    def _run(report: object, config: object, **kwargs: object) -> ProposalRunSummary:
        calls.append({"report": report, **kwargs})
        return summary

    recorded = [_RECORDED_EVENT] if events is None else events
    monkeypatch.setattr(flow, "run_triage_proposals", _run)
    monkeypatch.setattr(headless, "_recorded_events", lambda root, run_id: recorded)
    return calls


class TestHeadlessFlow:
    def test_writes_a_pending_decision_and_applies_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_flow(monkeypatch, _summary())
        import theforge.coordinator.triage_ratification_flow as ratify_flow

        monkeypatch.setattr(
            ratify_flow,
            "ratify_triage_run",
            lambda *a, **k: pytest.fail("headless triage must never ratify"),
        )

        outcome = headless.run_headless_triage(
            _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
        )
        assert outcome.status == headless.HEADLESS_WRITTEN
        assert outcome.pending_id == "triage-run123"
        assert outcome.findings_count == 1
        assert outcome.flagged_count == 1

        entry = _pending.find_triage_pending("run123", tmp_path)
        assert entry is not None
        assert entry["findings_count"] == 1
        assert entry["flagged_count"] == 1
        assert entry["report_path"] == "/tmp/backlog.yaml"
        assert "run_summary" in entry
        assert entry["proposals"][0]["finding_id"] == "1312:audit-count"
        assert "decision" not in entry

    def test_package_carries_proposals_reviews_and_evidence_refs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_flow(monkeypatch, _summary())
        monkeypatch.setattr(
            headless,
            "_recorded_events",
            lambda root, run_id: [
                {
                    "finding_id": "1312:audit-count",
                    "issue_ref": "#1312",
                    "disposition": "punt",
                    "punt_reason_code": "verified-stale",
                    "evidence_refs": ["symbol-absent"],
                    "proposal": {"disposition": "punt", "rationale": "why"},
                    "punt_review": {
                        "verdict": "challenge",
                        "evidence_refs": ["path-churn"],
                        "rationale": "not so fast",
                    },
                }
            ],
        )
        headless.run_headless_triage(
            _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
        )
        entry = _pending.find_triage_pending("run123", tmp_path)
        assert entry is not None
        assert entry["proposals"][0]["disposition"] == "punt"
        assert entry["punt_reviews"][0]["verdict"] == "challenge"
        assert entry["evidence_refs"] == ["symbol-absent", "path-churn"]

    def test_all_agent_failure_fallbacks_are_persisted_and_reported_as_unreviewed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import theforge.coordinator.triage_proposal_flow as flow

        summary = _summary(
            results=(
                FindingProposalResult(
                    finding_id="1312:audit-count",
                    issue_ref="#1312",
                    packet_hash="abc",
                    proposal=needs_verification_proposal(
                        _packet(), basis="proposer failed validation"
                    ),
                    fallback_reason=flow.FALLBACK_INVALID_OUTPUT,
                ),
                FindingProposalResult(
                    finding_id="1444:missing-evidence",
                    issue_ref="#1444",
                    packet_hash="def",
                    proposal=needs_verification_proposal(
                        FindingPacket(
                            finding_id="1444:missing-evidence",
                            issue_ref="#1444",
                            finding_body="missing checkable evidence",
                        ),
                        basis="no checkable artifact cited",
                    ),
                    fallback_reason=flow.FALLBACK_NO_CHECKABLE_EVIDENCE,
                ),
            ),
            total_cost_usd=0.0,
            review_stage=PuntReviewStage(),
        )
        _stub_flow(
            monkeypatch,
            summary,
            events=[
                {
                    "finding_id": "1312:audit-count",
                    "issue_ref": "#1312",
                    "disposition": "needs_verification",
                    "fallback_reason": flow.FALLBACK_INVALID_OUTPUT,
                    "proposal": {
                        "disposition": "needs_verification",
                        "rationale": "",
                    },
                },
                {
                    "finding_id": "1444:missing-evidence",
                    "issue_ref": "#1444",
                    "disposition": "needs_verification",
                    "fallback_reason": flow.FALLBACK_NO_CHECKABLE_EVIDENCE,
                    "proposal": {
                        "disposition": "needs_verification",
                        "rationale": "",
                    },
                },
            ],
        )

        outcome = headless.run_headless_triage(
            _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
        )

        assert "degraded to 2 fallback disposition(s)" in outcome.lines[0]
        assert "without accepted proposer review" in outcome.lines[0]
        entry = _pending.find_triage_pending("run123", tmp_path)
        assert entry is not None
        assert entry["all_findings_unreviewed"] is True
        assert entry["accepted_proposal_count"] == 0
        assert entry["agent_failure_fallback_count"] == 1
        assert entry["no_checkable_evidence_count"] == 1
        assert "without accepted proposer review" in entry["reason"]
        assert "awaiting operator decision" in entry["reason"]
        assert entry["proposals"][0]["fallback_reason"] == flow.FALLBACK_INVALID_OUTPUT

    def test_legitimate_no_checkable_evidence_path_stays_a_normal_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import theforge.coordinator.triage_proposal_flow as flow

        summary = _summary(
            results=(
                FindingProposalResult(
                    finding_id="1312:audit-count",
                    issue_ref="#1312",
                    packet_hash="abc",
                    proposal=needs_verification_proposal(
                        _packet(), basis="no checkable artifact cited"
                    ),
                    fallback_reason=flow.FALLBACK_NO_CHECKABLE_EVIDENCE,
                ),
            ),
            total_cost_usd=0.0,
            review_stage=PuntReviewStage(),
        )
        _stub_flow(
            monkeypatch,
            summary,
            events=[
                {
                    "finding_id": "1312:audit-count",
                    "issue_ref": "#1312",
                    "disposition": "needs_verification",
                    "fallback_reason": flow.FALLBACK_NO_CHECKABLE_EVIDENCE,
                    "proposal": {
                        "disposition": "needs_verification",
                        "rationale": "",
                    },
                }
            ],
        )

        outcome = headless.run_headless_triage(
            _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
        )

        assert outcome.lines[0] == "triage: proposal pass proposed 1 disposition(s)"
        entry = _pending.find_triage_pending("run123", tmp_path)
        assert entry is not None
        assert entry["all_findings_unreviewed"] is False
        assert entry["accepted_proposal_count"] == 0
        assert entry["agent_failure_fallback_count"] == 0
        assert entry["no_checkable_evidence_count"] == 1
        assert entry["reason"].endswith("awaiting operator ratification")

    def test_supersession_names_the_pending_run_and_spends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pending.write_triage_pending("earlier", {"findings_count": 4}, project_root=tmp_path)
        calls = _stub_flow(monkeypatch, _summary())

        outcome = headless.run_headless_triage(
            _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
        )
        assert outcome.status == headless.HEADLESS_SUPERSEDED
        assert "triage-earlier" in outcome.message
        assert calls == []
        assert _pending.find_triage_pending("run123", tmp_path) is None

    def test_no_report_collects_and_writes_one_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import theforge.triage_backlog_report as backlog_mod
        import theforge.triage_report as report_mod

        seen: dict[str, object] = {}
        monkeypatch.setattr(
            backlog_mod,
            "collect_backlog_report",
            lambda root, current_milestone=None: seen.setdefault("collected", True) or object(),
        )
        monkeypatch.setattr(
            backlog_mod,
            "write_backlog_report",
            lambda root, report, output_path=None: tmp_path / "backlog.yaml",
        )
        monkeypatch.setattr(report_mod, "load_backlog_report", lambda path: _StubReport())
        calls = _stub_flow(monkeypatch, _summary())

        outcome = headless.run_headless_triage(_FakeConfig(tmp_path), project_root=tmp_path)
        assert outcome.status == headless.HEADLESS_WRITTEN
        assert seen["collected"] is True
        assert len(calls) == 1

    def test_an_empty_backlog_still_persists_despite_recording_no_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No events is correct for a run with no findings — not a failure."""
        _stub_flow(
            monkeypatch,
            _summary(results=(), total_cost_usd=0.0, review_stage=PuntReviewStage()),
            events=[],
        )
        outcome = headless.run_headless_triage(
            _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
        )
        assert outcome.status == headless.HEADLESS_WRITTEN
        assert outcome.findings_count == 0
        assert _pending.find_triage_pending("run123", tmp_path) is not None

    def test_run_level_dispatch_failure_returns_failed_outcome_and_writes_no_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failure = (
            "triage aborted agent dispatch before any proposer ran: the selected Claude "
            "profile could not authenticate with the same proposer environment triage "
            "would pass to the CLI (shell environment overlaid by project .forge/.env "
            "secrets). claude credential store at /tmp/stale/.credentials.json holds no "
            "access or refresh token"
        )
        _stub_flow(monkeypatch, _summary(total_cost_usd=0.0, run_level_failure=failure))
        monkeypatch.setattr(
            headless,
            "_recorded_events",
            lambda root, run_id: pytest.fail("headless triage must not read events on auth gate"),
        )

        outcome = headless.run_headless_triage(
            _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
        )

        assert outcome.status == headless.HEADLESS_FAILED
        assert outcome.message == failure
        assert outcome.error == failure
        assert outcome.lines == (f"triage: {failure}",)
        assert _pending.find_triage_pending("run123", tmp_path) is None

    def test_report_collection_failure_raises_a_headless_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import theforge.triage_backlog_report as backlog_mod

        monkeypatch.setattr(
            backlog_mod,
            "collect_backlog_report",
            lambda *a, **k: (_ for _ in ()).throw(
                backlog_mod.TriageBacklogReportError("gh api failed")
            ),
        )
        with pytest.raises(headless.HeadlessTriageError, match="gh api failed"):
            headless.run_headless_triage(_FakeConfig(tmp_path), project_root=tmp_path)


class TestUnratifiableRuns:
    """A package the operator could not ratify is never published (#2231, cycle 1).

    ``ratify_triage_run`` reads the run and its proposal events out of the audit
    substrate and refuses a run it cannot find — so a pending decision whose
    recording did not survive promises an operator something forge cannot keep.
    """

    def test_an_audit_write_failure_writes_no_pending_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_flow(monkeypatch, _summary(audit_error="disk full"))

        with pytest.raises(headless.HeadlessTriageError, match="disk full"):
            headless.run_headless_triage(
                _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
            )
        assert _pending.find_triage_pending("run123", tmp_path) is None
        assert _pending.unresolved_triage_pending(tmp_path) is None

    def test_an_unreadable_recording_writes_no_pending_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The writer reported success but the events cannot be read back."""
        _stub_flow(monkeypatch, _summary(), events=[])

        with pytest.raises(headless.HeadlessTriageError, match="could not be read back"):
            headless.run_headless_triage(
                _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
            )
        assert _pending.find_triage_pending("run123", tmp_path) is None

    def test_the_failure_names_the_run_and_what_it_proposed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The spend already happened; the message must not read as a no-op."""
        _stub_flow(monkeypatch, _summary(audit_error="disk full"))

        with pytest.raises(headless.HeadlessTriageError) as excinfo:
            headless.run_headless_triage(
                _FakeConfig(tmp_path), project_root=tmp_path, report=_StubReport()
            )
        message = str(excinfo.value)
        assert "run123" in message
        assert "1 finding(s)" in message

    def test_a_recording_failure_never_fails_the_sprint_that_triggered_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import theforge.triage_backlog_report as backlog_mod
        import theforge.triage_report as report_mod
        from theforge.sprint.post_sprint_triage import run_post_sprint_triage

        monkeypatch.setattr(
            backlog_mod, "collect_backlog_report", lambda root, current_milestone=None: object()
        )
        monkeypatch.setattr(
            backlog_mod,
            "write_backlog_report",
            lambda root, report, output_path=None: tmp_path / "backlog.yaml",
        )
        monkeypatch.setattr(report_mod, "load_backlog_report", lambda path: _StubReport())
        _stub_flow(monkeypatch, _summary(audit_error="disk full"))

        outcome = run_post_sprint_triage(_StubState(_FakeConfig(tmp_path)))
        assert outcome.status == headless.HEADLESS_FAILED
        assert "disk full" in outcome.error
        assert _pending.find_triage_pending("run123", tmp_path) is None
        combined = capsys.readouterr()
        assert "disk full" in (combined.out + combined.err)


# ── Status rendering ─────────────────────────────────────────────────────────


class TestStatusRendering:
    def test_triage_entry_shows_date_findings_flagged_age_and_commands(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge.cli import status as cli_status

        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
        pending_dir = tmp_path / ".forge" / "pending"
        pending_dir.mkdir(parents=True)
        (pending_dir / "triage-run123.yaml").write_text(
            yaml.safe_dump(
                {
                    "kind": "triage",
                    "run_id": "triage-run123",
                    "triage_run_id": "run123",
                    "created_at": created.isoformat(),
                    "findings_count": 15,
                    "flagged_count": 2,
                }
            ),
            encoding="utf-8",
        )

        cli_status._show_pending_decisions(_pending, tmp_path)
        out = capsys.readouterr().out
        assert "triage" in out
        assert "15 findings" in out
        assert "(2 flagged)" in out
        assert "age 3h" in out
        assert "forge triage --ratify run123" in out
        assert "forge triage --discard triage-run123" in out

    def test_hitl_entries_still_render_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge.cli import status as cli_status

        _pending.write_pending(
            run_id="story-a",
            story="story-a",
            phase="HUMAN_REVIEW",
            reason="APPROVE (0 P1, 1 P2)",
            options=["approve", "reject"],
            timeout_seconds=600,
            project_root=tmp_path,
        )
        cli_status._show_pending_decisions(_pending, tmp_path)
        out = capsys.readouterr().out
        assert "story-a  [HUMAN_REVIEW]  story=story-a" in out
        assert "options: approve/reject" in out

    def test_decide_refuses_a_triage_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import argparse

        from theforge.cli import status as cli_status

        _pending.write_triage_pending("run123", {"findings_count": 3}, project_root=tmp_path)
        monkeypatch.setattr(cli_status, "_find_config", lambda *a, **k: None)
        monkeypatch.chdir(tmp_path)

        code = cli_status.cmd_decide(argparse.Namespace(run_id="triage-run123", action="approve"))
        err = capsys.readouterr().err
        assert code == 1
        assert "forge triage --ratify run123" in err
        # No generic decision field was written.
        entry = _pending.find_triage_pending("run123", tmp_path)
        assert entry is not None
        assert "decision" not in entry


# ── Config ───────────────────────────────────────────────────────────────────


class TestPostSprintTriageConfig:
    def test_defaults_off(self, tmp_path: Path) -> None:
        from theforge.config import load_config

        path = tmp_path / "forge.yaml"
        path.write_text("project: demo\n", encoding="utf-8")
        assert load_config(path).sprint.post_sprint_triage is False

    def test_enabled_when_declared(self, tmp_path: Path) -> None:
        from theforge.config import load_config

        path = tmp_path / "forge.yaml"
        path.write_text("project: demo\nsprint:\n  post_sprint_triage: true\n", encoding="utf-8")
        assert load_config(path).sprint.post_sprint_triage is True

    def test_non_boolean_is_a_config_error(self, tmp_path: Path) -> None:
        from theforge.config import load_config

        path = tmp_path / "forge.yaml"
        path.write_text(
            "project: demo\nsprint:\n  post_sprint_triage: yes-please\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="post_sprint_triage"):
            load_config(path)


# ── Post-sprint helper ───────────────────────────────────────────────────────


class _StubContext:
    def __init__(self, config: object) -> None:
        self.config = config


class _StubState:
    def __init__(self, config: object) -> None:
        self.context = _StubContext(config)


class TestPostSprintHelper:
    def test_enabled_pass_writes_a_pending_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import theforge.triage_backlog_report as backlog_mod
        import theforge.triage_report as report_mod
        from theforge.sprint.post_sprint_triage import run_post_sprint_triage

        monkeypatch.setattr(
            backlog_mod, "collect_backlog_report", lambda root, current_milestone=None: object()
        )
        monkeypatch.setattr(
            backlog_mod,
            "write_backlog_report",
            lambda root, report, output_path=None: tmp_path / "backlog.yaml",
        )
        monkeypatch.setattr(report_mod, "load_backlog_report", lambda path: _StubReport())
        _stub_flow(monkeypatch, _summary())

        outcome = run_post_sprint_triage(_StubState(_FakeConfig(tmp_path)))
        assert outcome.status == headless.HEADLESS_WRITTEN
        assert _pending.find_triage_pending("run123", tmp_path) is not None

    def test_supersession_is_reported_in_the_sprint_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge.sprint.post_sprint_triage import run_post_sprint_triage

        _pending.write_triage_pending("earlier", {"findings_count": 4}, project_root=tmp_path)
        calls = _stub_flow(monkeypatch, _summary())

        outcome = run_post_sprint_triage(_StubState(_FakeConfig(tmp_path)))
        assert outcome.status == headless.HEADLESS_SUPERSEDED
        assert calls == []
        combined = capsys.readouterr()
        assert "triage-earlier" in (combined.out + combined.err)

    def test_a_failing_pass_is_reported_and_never_escapes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from theforge.sprint.post_sprint_triage import run_post_sprint_triage

        monkeypatch.setattr(
            headless,
            "run_headless_triage",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("substrate exploded")),
        )
        outcome = run_post_sprint_triage(_StubState(_FakeConfig(tmp_path)))
        assert outcome.status == headless.HEADLESS_FAILED
        assert "substrate exploded" in outcome.error
        combined = capsys.readouterr()
        assert "substrate exploded" in (combined.out + combined.err)

    def test_runner_calls_the_helper_only_when_the_flag_is_on(self) -> None:
        """The trigger is opt-in at the call site, not a default inside the helper."""
        import ast
        import inspect

        from theforge.sprint import runner

        source = inspect.getsource(runner.run_sprint)
        tree = ast.parse(source.lstrip())
        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and "post_sprint_triage" in ast.dump(node.test)
            and "run_post_sprint_triage" in ast.dump(node)
        ]
        assert guarded, "post-sprint triage must be guarded by config.sprint.post_sprint_triage"
