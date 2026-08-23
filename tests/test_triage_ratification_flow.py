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
)
from theforge.triage_report import parse_backlog_report

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


def _live_report(*, changed: bool = False, findings: int = 1) -> BacklogTriageReport:
    records = []
    for i in range(findings):
        records.append(
            BacklogFindingRecord(
                finding_id=f"#131{i}",
                issue_ref=f"#131{i}",
                issue_number=1310 + i,
                title="audit count is off by one",
                body="audit count is off by one",
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
    summary = proposal_flow.run_triage_proposals(
        _proposal_report(findings=findings),
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

    def test_ratify_names_the_no_audit_case_explicitly(self, tmp_path: Path) -> None:
        with pytest.raises(ratify_flow.TriageRatificationError, match="--no-audit"):
            ratify_flow.ratify_triage_run(
                "missing-run",
                _config(tmp_path),
                project_root=tmp_path,
                input_fn=_inputs(),
                emit=lambda _line: None,
            )
