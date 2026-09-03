"""Tests for the operator-ratified ``forge triage`` application flow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from theforge.coordinator import audit_read_model, audit_storage
from theforge.coordinator import triage_proposal_flow as proposal_flow
from theforge.coordinator import triage_ratification_flow as ratify_flow
from theforge.triage_backlog_report import (
    HYGIENE_POOL,
    BacklogFindingRecord,
    BacklogTriageReport,
    EvidenceEntry,
    write_backlog_report,
)
from theforge.triage_report import load_backlog_report, parse_backlog_report

_VALID_PUNT = (
    "<triage_proposal>\n"
    "disposition: punt\n"
    "punt_reason_code: verified-stale\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
    "</triage_proposal>"
)

_VALID_REVIEW_CONCUR = (
    "<triage_punt_review>\n"
    "verdict: concur\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
    "</triage_punt_review>"
)


def _config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
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


class _StubResult:
    def __init__(self, output: str, cost_usd: float = 0.01) -> None:
        self.output = output
        self.success = True
        self.cost_usd = cost_usd
        self.cost_provenance = "provider_reported"


class _StubRunner:
    def __init__(self, findings: int = 1) -> None:
        self.findings = findings
        self.proposals = 0
        self.reviews = 0

    def __call__(self, **kwargs: object) -> _StubResult:
        prompt = str(kwargs.get("prompt", ""))
        if "ADVERSARIAL PUNT REVIEWER" in prompt:
            self.reviews += 1
            return _StubResult(_VALID_REVIEW_CONCUR)
        self.proposals += 1
        return _StubResult(_VALID_PUNT)


def _proposal_report(*, findings: int = 1) -> object:
    return parse_backlog_report(
        {
            "current_milestone": "v0.12.0",
            "named_milestones": ["v0.13.0"],
            "findings": [
                {
                    "finding_id": f"131{i}:audit-count",
                    "issue_ref": f"#131{i}",
                    "issue_number": 1310 + i,
                    "title": "audit count is off by one",
                    "body": "audit count is off by one",
                    "labels": ["bug", "forge-finding"],
                    "pool_state": "Hygiene",
                    "verification_status": "stale_evidence",
                    "evidence": [
                        {
                            "id": "symbol-absent",
                            "kind": "staleness",
                            "summary": "cited symbol absent from current tree",
                            "checkable": True,
                        }
                    ],
                }
                for i in range(findings)
            ],
        },
        source_path="backlog.json",
    )


def _live_report(
    *,
    changed: bool = False,
    findings: int = 1,
    body: str = "audit count is off by one",
) -> BacklogTriageReport:
    records = []
    for i in range(findings):
        records.append(
            BacklogFindingRecord(
                finding_id=f"131{i}:audit-count",
                issue_ref=f"#131{i}",
                issue_number=1310 + i,
                title="audit count is off by one",
                body=body,
                labels=("bug", "forge-finding") if not changed else ("bug", "p2"),
                display_labels="bug" if not changed else "bug,p2",
                opened_at="2026-05-31T00:00:00Z",
                age_days=84,
                pool_state="Hygiene",
                verification_status="stale_evidence" if not changed else "active",
                evidence=(
                    EvidenceEntry(
                        evidence_id="symbol-absent",
                        kind="staleness",
                        summary=(
                            "cited symbol absent from current tree"
                            if not changed
                            else "cited symbol present in current tree"
                        ),
                        checkable=True,
                        observed_status="stale_evidence" if not changed else "active",
                    ),
                ),
            )
        )
    return BacklogTriageReport(
        generated_at="2026-08-23T00:00:00Z",
        current_milestone="v0.12.0",
        named_milestones=("v0.13.0", HYGIENE_POOL),
        findings=tuple(records),
    )


def _seed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, findings: int = 1) -> str:
    runner = _StubRunner(findings=findings)
    monkeypatch.setattr(proposal_flow, "run_agent", runner)
    monkeypatch.setattr(proposal_flow, "log_agent_result", lambda *a, **k: None)
    monkeypatch.setattr(
        proposal_flow, "_select_advisor_profile", lambda config: DEFAULT_PREFLIGHT_PROFILE
    )
    summary = proposal_flow._run_triage_proposals_impl(
        _proposal_report(findings=findings),
        _config(tmp_path),
        record=True,
    )
    return summary.triage_run_id


def _seed_run_from_generated_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: BacklogTriageReport,
) -> str:
    runner = _StubRunner(findings=len(report.findings))
    monkeypatch.setattr(proposal_flow, "run_agent", runner)
    monkeypatch.setattr(proposal_flow, "log_agent_result", lambda *a, **k: None)
    monkeypatch.setattr(
        proposal_flow, "_select_advisor_profile", lambda config: DEFAULT_PREFLIGHT_PROFILE
    )
    artifact = write_backlog_report(
        tmp_path,
        report,
        output_path=tmp_path / ".forge" / "triage" / "backlog.yaml",
    )
    summary = proposal_flow._run_triage_proposals_impl(
        load_backlog_report(artifact),
        _config(tmp_path),
        record=True,
    )
    return summary.triage_run_id


def _inputs(*answers: str):
    queue = iter(answers)
    return lambda _prompt="": next(queue)


def _application_rows(tmp_path: Path) -> list[dict]:
    conn = audit_storage.open_readonly(tmp_path)
    try:
        return list(audit_read_model.iter_triage_application_records(conn))
    finally:
        conn.close()


def _proposal_events(tmp_path: Path, triage_run_id: str) -> list[dict]:
    conn = audit_storage.open_readonly(tmp_path)
    try:
        return list(
            audit_read_model.iter_triage_proposal_events(
                conn,
                triage_run_id=triage_run_id,
            )
        )
    finally:
        conn.close()


class TestRatificationFlow:
    def test_accept_applies_the_reviewed_punt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        comment_bodies: list[str] = []
        closed: list[int] = []

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_view",
            lambda number, root: {
                "number": number,
                "state": "OPEN",
                "milestone": None,
                "comments": [],
            },
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_comment",
            lambda number, body, root: comment_bodies.append(body),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_close",
            lambda number, root: closed.append(number),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("accept", "operator concurs"),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "applied"
        assert closed == [1310]
        assert len(comment_bodies) == 1
        assert "forge-triage:" + run_id in comment_bodies[0]
        assert "Reason code: verified-stale" in comment_bodies[0]
        assert 'symbol-absent: "cited symbol absent from current tree"' in comment_bodies[0]
        assert "Operator note: operator concurs" in comment_bodies[0]

        rows = _application_rows(tmp_path)
        assert rows[0]["status"] == "applied"
        assert rows[0]["operator_decision"] == "accept"
        assert rows[0]["applied_disposition"] == "punt"

    def test_round_tripped_generated_report_does_not_trip_stale_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live_report = _live_report(body="  audit count is off by one  ")
        run_id = _seed_run_from_generated_report(tmp_path, monkeypatch, live_report)
        closed: list[int] = []

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: live_report,
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_view",
            lambda number, root: {
                "number": number,
                "state": "OPEN",
                "milestone": None,
                "comments": [],
            },
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_comment",
            lambda number, body, root: None,
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_close",
            lambda number, root: closed.append(number),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("accept", "round trip"),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "applied"
        assert summary.findings[0].stale_reason == ""
        assert closed == [1310]

    def test_override_can_apply_hygiene_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        edits: list[tuple[int, str]] = []

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_view",
            lambda number, root: {
                "number": number,
                "state": "OPEN",
                "milestone": None,
                "comments": [],
            },
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_edit_milestone",
            lambda number, milestone, root: edits.append((number, milestone)),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("override", "fix_later", HYGIENE_POOL, "", "reviewer is right"),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "applied"
        assert summary.findings[0].target_milestone == HYGIENE_POOL
        assert edits == [(1310, HYGIENE_POOL)]
        rows = _application_rows(tmp_path)
        assert rows[0]["operator_decision"] == "override"
        assert rows[0]["applied_disposition"] == "fix_later"
        assert rows[0]["target_milestone"] == HYGIENE_POOL

    def test_punt_override_without_a_recognized_reason_code_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("override", "punt", "bad-code", "", "bad override", "skip", ""),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "skipped"
        rows = _application_rows(tmp_path)
        assert rows[0]["status"] == "skipped"

    def test_punt_override_without_renderable_evidence_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        conn = audit_storage.create_or_open(tmp_path)
        try:
            row = conn.execute("SELECT raw_json FROM triage_proposal_events").fetchone()
            payload = json.loads(row[0])
            proposal = dict(payload["proposal"])
            proposal["evidence_refs"] = []
            proposal["citations"] = []
            payload["proposal"] = proposal
            payload["evidence_refs"] = []
            conn.execute(
                "UPDATE triage_proposal_events SET raw_json = ?",
                (json.dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )

        emitted: list[str] = []
        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs(
                "override",
                "punt",
                "verified-stale",
                "",
                "blank evidence",
                "skip",
                "",
            ),
            emit=emitted.append,
        )

        assert summary.findings[0].status == "skipped"
        assert any("punt requires at least one cited evidence ref" in line for line in emitted)
        rows = _application_rows(tmp_path)
        assert rows[0]["status"] == "skipped"

    def test_changed_live_state_is_marked_stale_and_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        closed: list[int] = []

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(changed=True),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_view",
            lambda number, root: {
                "number": number,
                "state": "OPEN",
                "milestone": None,
                "comments": [],
            },
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_close",
            lambda number, root: closed.append(number),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("accept", "stale now"),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "stale"
        assert closed == []
        rows = _application_rows(tmp_path)
        assert rows[0]["status"] == "stale"
        assert "diverged from the reviewed snapshot" in rows[0]["stale_reason"]

    def test_fix_target_already_present_still_checks_for_stale_live_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        edits: list[tuple[int, str]] = []

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(changed=True),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_view",
            lambda number, root: {
                "number": number,
                "state": "OPEN",
                "milestone": {"title": HYGIENE_POOL},
                "comments": [],
            },
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_edit_milestone",
            lambda number, milestone, root: edits.append((number, milestone)),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("override", "fix_later", HYGIENE_POOL, "", "already moved"),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "stale"
        assert edits == []
        rows = _application_rows(tmp_path)
        assert rows[0]["status"] == "stale"
        assert "diverged from the reviewed snapshot" in rows[0]["stale_reason"]

    def test_missing_snapshot_is_treated_as_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        conn = audit_storage.create_or_open(tmp_path)
        try:
            row = conn.execute("SELECT raw_json FROM triage_proposal_events").fetchone()
            payload = json.loads(row[0])
            payload.pop("finding_snapshot", None)
            payload.pop("finding_snapshot_digest", None)
            conn.execute(
                "UPDATE triage_proposal_events SET raw_json = ?",
                (json.dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_view",
            lambda number, root: {
                "number": number,
                "state": "OPEN",
                "milestone": None,
                "comments": [],
            },
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("accept", "old run"),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "stale"
        assert "predates stored finding snapshots" in summary.findings[0].stale_reason

    def test_missing_packet_and_snapshot_are_treated_as_stale_without_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        conn = audit_storage.create_or_open(tmp_path)
        try:
            row = conn.execute("SELECT raw_json FROM triage_proposal_events").fetchone()
            payload = json.loads(row[0])
            payload.pop("packet", None)
            payload.pop("finding_snapshot", None)
            payload.pop("finding_snapshot_digest", None)
            conn.execute(
                "UPDATE triage_proposal_events SET raw_json = ?",
                (json.dumps(payload),),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=lambda _prompt="": (_ for _ in ()).throw(
                AssertionError("legacy proposal rows should not be re-prompted")
            ),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "stale"
        assert "predates stored finding packets and snapshots" in summary.findings[0].stale_reason

    def test_resume_reuses_the_comment_marker_and_finishes_remaining_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        state = {"comments": [], "close_calls": 0}

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )

        def _gh_issue_view(number: int, root: Path) -> dict:
            return {
                "number": number,
                "state": "OPEN",
                "milestone": None,
                "comments": [{"body": body} for body in state["comments"]],
            }

        def _gh_issue_comment(number: int, body: str, root: Path) -> None:
            state["comments"].append(body)

        def _gh_issue_close(number: int, root: Path) -> None:
            state["close_calls"] += 1
            if state["close_calls"] == 1:
                raise ratify_flow.TriageRatificationError("close failed")

        monkeypatch.setattr(ratify_flow, "_gh_issue_view", _gh_issue_view)
        monkeypatch.setattr(ratify_flow, "_gh_issue_comment", _gh_issue_comment)
        monkeypatch.setattr(ratify_flow, "_gh_issue_close", _gh_issue_close)

        first = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("accept", "first try"),
            emit=lambda _line: None,
        )
        assert first.findings[0].status == "failed"
        assert len(state["comments"]) == 1

        second = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=lambda _prompt="": (_ for _ in ()).throw(
                AssertionError("should not re-prompt")
            ),
            emit=lambda _line: None,
        )
        assert second.findings[0].status == "applied"
        assert len(state["comments"]) == 1
        assert state["close_calls"] == 2

    def test_missing_issue_number_marks_only_that_finding_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch, findings=2)
        conn = audit_storage.create_or_open(tmp_path)
        try:
            rows = conn.execute(
                "SELECT finding_id, raw_json FROM triage_proposal_events ORDER BY finding_id"
            ).fetchall()
            payload = json.loads(rows[0][1])
            payload["issue_ref"] = "audit-count-without-number"
            snapshot = payload.get("finding_snapshot")
            if isinstance(snapshot, dict):
                snapshot.pop("issue_number", None)
            conn.execute(
                "UPDATE triage_proposal_events SET raw_json = ? WHERE finding_id = ?",
                (json.dumps(payload), rows[0][0]),
            )
            conn.commit()
        finally:
            conn.close()

        state: dict[int, dict[str, object]] = {}
        comment_numbers: list[int] = []
        closed_numbers: list[int] = []

        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(findings=2),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )

        def _gh_issue_view(number: int, root: Path) -> dict:
            issue_state = state.setdefault(
                number,
                {"state": "OPEN", "comments": []},
            )
            return {
                "number": number,
                "state": issue_state["state"],
                "milestone": None,
                "comments": [{"body": body} for body in issue_state["comments"]],
            }

        def _gh_issue_comment(number: int, body: str, root: Path) -> None:
            state.setdefault(number, {"state": "OPEN", "comments": []})["comments"].append(body)
            comment_numbers.append(number)

        def _gh_issue_close(number: int, root: Path) -> None:
            state.setdefault(number, {"state": "OPEN", "comments": []})["state"] = "CLOSED"
            closed_numbers.append(number)

        monkeypatch.setattr(ratify_flow, "_gh_issue_view", _gh_issue_view)
        monkeypatch.setattr(ratify_flow, "_gh_issue_comment", _gh_issue_comment)
        monkeypatch.setattr(ratify_flow, "_gh_issue_close", _gh_issue_close)

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=_inputs("accept", "missing number", "accept", "apply second"),
            emit=lambda _line: None,
        )

        assert [finding.status for finding in summary.findings] == ["failed", "applied"]
        assert "has no usable issue number" in summary.findings[0].summary
        assert summary.findings[1].issue_ref == "#1311"
        assert comment_numbers == [1311]
        assert closed_numbers == [1311]
        rows = _application_rows(tmp_path)
        assert [row["status"] for row in rows] == ["failed", "applied"]
        assert "has no usable issue number" in rows[0]["external_effect_summary"]
        assert rows[1]["external_effect_summary"] == "Posted closing comment and closed the issue."

    def test_resume_recognizes_a_closed_marked_punt_as_already_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_id = _seed_run(tmp_path, monkeypatch)
        event = _proposal_events(tmp_path, run_id)[0]
        marker = ratify_flow._idempotency_marker(run_id, str(event["finding_id"]))

        audit_storage.upsert_triage_application_record(
            tmp_path,
            {
                "triage_run_id": run_id,
                "finding_id": str(event["finding_id"]),
                "issue_ref": str(event["issue_ref"]),
                "proposed_payload": dict(event["proposal"]),
                "operator_decision": "accept",
                "applied_disposition": "punt",
                "target_milestone": None,
                "punt_reason_code": "verified-stale",
                "evidence_refs": ["symbol-absent"],
                "operator_note": "resume after interruption",
                "status": "failed",
                "stale_reason": "",
                "idempotency_marker": marker,
                "external_effect_summary": "close interrupted after GitHub mutation",
            },
        )

        comment_bodies: list[str] = []
        closed: list[int] = []
        monkeypatch.setattr(
            ratify_flow,
            "collect_backlog_report",
            lambda *a, **k: _live_report(),
        )
        monkeypatch.setattr(
            ratify_flow,
            "fetch_open_milestones",
            lambda *_: ("v0.13.0", HYGIENE_POOL),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_view",
            lambda number, root: {
                "number": number,
                "state": "CLOSED",
                "milestone": None,
                "comments": [{"body": f"forge triage ratification\nMarker: {marker}"}],
            },
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_comment",
            lambda number, body, root: comment_bodies.append(body),
        )
        monkeypatch.setattr(
            ratify_flow,
            "_gh_issue_close",
            lambda number, root: closed.append(number),
        )

        summary = ratify_flow.ratify_triage_run(
            run_id,
            _config(tmp_path),
            project_root=tmp_path,
            input_fn=lambda _prompt="": (_ for _ in ()).throw(
                AssertionError("should not re-prompt")
            ),
            emit=lambda _line: None,
        )

        assert summary.findings[0].status == "applied"
        assert (
            summary.findings[0].summary
            == "Closing comment already posted and issue already closed."
        )
        assert comment_bodies == []
        assert closed == []
        rows = _application_rows(tmp_path)
        assert rows[0]["status"] == "applied"
        assert rows[0]["stale_reason"] == ""

    def test_ratify_names_the_no_audit_case_explicitly(self, tmp_path: Path) -> None:
        with pytest.raises(ratify_flow.TriageRatificationError, match="--no-audit"):
            ratify_flow.ratify_triage_run(
                "missing-run",
                _config(tmp_path),
                project_root=tmp_path,
                input_fn=_inputs(),
                emit=lambda _line: None,
            )


class TestSpikeClosureGuard:
    """Ratification closes issues, so it asks the spike guard first (#2600)."""

    @staticmethod
    def _gh_view(labels: tuple[str, ...], body: str = ""):
        import subprocess

        payload = json.dumps(
            {
                "state": "OPEN",
                "labels": [{"name": name} for name in labels],
                "body": body,
                "comments": [],
            }
        )

        def _run(cmd, **kwargs):
            if "view" in cmd:
                return subprocess.CompletedProcess(cmd, 0, payload, "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        return _run

    def test_outcomeless_spike_fails_the_close(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "theforge.spike_guard.guard.subprocess.run",
            self._gh_view(("spike",), "A question."),
        )
        with pytest.raises(ratify_flow.TriageRatificationError, match="records no outcome"):
            ratify_flow._gh_issue_close(2348, tmp_path)

    def test_non_spike_closes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "theforge.spike_guard.guard.subprocess.run", self._gh_view(("enhancement",))
        )
        closed: list[list[str]] = []

        def _run(cmd, **kwargs):
            import subprocess

            closed.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("theforge.coordinator.triage_ratification_flow.subprocess.run", _run)
        ratify_flow._gh_issue_close(1310, tmp_path)
        assert any("close" in cmd for cmd in closed)
