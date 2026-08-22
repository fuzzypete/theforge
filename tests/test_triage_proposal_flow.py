"""Tests for the ``forge triage`` proposal flow: retries, fallbacks, spend, audit.

The deterministic pipeline is covered here end to end with a stubbed runner:
packet assembly (including the disposition history the substrate holds) →
agent invocation → validation → retry → the ``needs_verification`` fallback →
the audit rows and the reported spend. The schema boundary itself lives in
``test_triage_proposal.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from theforge.agent_types import COST_PROVIDER_REPORTED, COST_UNKNOWN
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
from theforge.coordinator import triage_proposal_flow as flow
from theforge.triage_proposal import (
    DISPOSITION_NEEDS_VERIFICATION,
    DISPOSITION_PUNT,
)
from theforge.triage_report import parse_backlog_report


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
    def __init__(self, output: str, cost_usd: float | None = 0.01, success: bool = True) -> None:
        self.output = output
        self.cost_usd = cost_usd
        self.success = success
        self.cost_provenance = COST_UNKNOWN if cost_usd is None else COST_PROVIDER_REPORTED


class _StubRunner:
    """Records every invocation and replays a scripted sequence of outputs."""

    def __init__(self, *results: _StubResult) -> None:
        self._results = list(results)
        self.prompts: list[str] = []

    def __call__(self, **kwargs: object) -> _StubResult:
        self.prompts.append(str(kwargs.get("prompt", "")))
        if not self._results:
            raise AssertionError("runner invoked more times than scripted")
        return self._results.pop(0)


def _install_runner(monkeypatch: pytest.MonkeyPatch, runner: object) -> None:
    monkeypatch.setattr(flow, "run_agent", runner)
    monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_select_advisor_profile", lambda config: DEFAULT_PREFLIGHT_PROFILE)


def _proposal_block(body: str) -> str:
    return f"<triage_proposal>\n{body}\n</triage_proposal>"


_VALID_PUNT = _proposal_block(
    "disposition: punt\n"
    "punt_reason_code: verified-stale\n"
    "evidence: report shows cited symbol absent from current tree\n"
    "evidence_refs: [symbol-absent]\n"
)

_INVALID = _proposal_block(
    "disposition: punt\n"
    "punt_reason_code: feels-old\n"
    "evidence: vibes\n"
    "evidence_refs: [symbol-absent]\n"
)


def _report(*, checkable: bool = True, findings: int = 1) -> object:
    return parse_backlog_report(
        {
            "current_milestone": "v0.12.0",
            "named_milestones": ["v0.13.0"],
            "findings": [
                {
                    "finding_id": f"131{i}:audit-count",
                    "issue_ref": f"#131{i}",
                    "body": "audit count is off by one",
                    "evidence": [
                        {
                            "id": "symbol-absent",
                            "kind": "staleness",
                            "summary": "cited symbol absent from current tree",
                            "checkable": checkable,
                        }
                    ],
                }
                for i in range(findings)
            ],
        },
        source_path="backlog.json",
    )


# ── Happy path ────────────────────────────────────────────────────────────────


class TestValidProposal:
    def test_valid_output_is_accepted_with_its_cost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(_StubResult(_VALID_PUNT, cost_usd=0.02))
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)

        assert summary.findings_count == 1
        result = summary.results[0]
        assert result.proposal.disposition == DISPOSITION_PUNT
        assert result.proposal.punt_reason_code == "verified-stale"
        assert result.cost_usd == pytest.approx(0.02)
        assert result.retry_count == 0
        assert result.fallback_reason == ""
        assert summary.total_cost_usd == pytest.approx(0.02)

    def test_per_finding_and_total_spend_are_both_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.02),
            _StubResult(_VALID_PUNT, cost_usd=0.03),
        )
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(findings=2), _config(tmp_path), record=False)
        assert [r.cost_usd for r in summary.results] == [
            pytest.approx(0.02),
            pytest.approx(0.03),
        ]
        assert summary.total_cost_usd == pytest.approx(0.05)
        assert summary.cost_provenance == COST_PROVIDER_REPORTED

    def test_an_unmeasured_attempt_taints_the_total_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(_StubResult(_VALID_PUNT, cost_usd=None))
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)
        assert summary.results[0].cost_usd is None
        assert summary.cost_provenance == COST_UNKNOWN


# ── Retry and fallback ────────────────────────────────────────────────────────


class TestRetryAndFallback:
    def test_invalid_output_is_retried_once_and_can_recover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_INVALID, cost_usd=0.01),
            _StubResult(_VALID_PUNT, cost_usd=0.01),
        )
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)
        result = summary.results[0]
        assert result.proposal.disposition == DISPOSITION_PUNT
        assert result.attempts == 2
        assert result.retry_count == 1
        assert result.cost_usd == pytest.approx(0.02)

    def test_the_retry_prompt_names_the_validator_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_INVALID, cost_usd=0.01),
            _StubResult(_VALID_PUNT, cost_usd=0.01),
        )
        _install_runner(monkeypatch, runner)

        flow.run_triage_proposals(_report(), _config(tmp_path), record=False)
        assert "previous attempt was REJECTED" in runner.prompts[1]
        assert "punt_reason_code must be one of" in runner.prompts[1]

    def test_persistently_invalid_output_becomes_needs_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_INVALID, cost_usd=0.01),
            _StubResult(_INVALID, cost_usd=0.01),
        )
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.proposal.disposition == DISPOSITION_NEEDS_VERIFICATION
        assert result.fallback_reason == flow.FALLBACK_INVALID_OUTPUT
        assert any("punt_reason_code" in e for e in result.validation_errors)
        assert result.retry_count == 1

    def test_invalid_output_is_never_guessed_into_the_disposition_it_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_INVALID, cost_usd=0.01), _StubResult(_INVALID, cost_usd=0.01)
        )
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.proposal.disposition != DISPOSITION_PUNT
        assert result.proposal.punt_reason_code is None

    def test_a_failed_agent_run_is_retried_then_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult("", cost_usd=0.0, success=False),
            _StubResult("", cost_usd=0.0, success=False),
        )
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.proposal.disposition == DISPOSITION_NEEDS_VERIFICATION
        assert result.attempts == 2

    def test_an_unavailable_proposer_falls_back_rather_than_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(flow, "run_agent", lambda **k: None)
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)

        def _no_model(config: object) -> object:
            raise ValueError("no configured model is phase-eligible for advisor")

        monkeypatch.setattr(flow, "_select_advisor_profile", _no_model)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.proposal.disposition == DISPOSITION_NEEDS_VERIFICATION
        assert result.fallback_reason == flow.FALLBACK_AGENT_UNAVAILABLE


# ── Deterministic paths that spend nothing ────────────────────────────────────


class TestDeterministicPaths:
    def test_no_checkable_evidence_proposes_needs_verification_without_an_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner()  # any invocation raises
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(
            _report(checkable=False), _config(tmp_path), record=False
        ).results[0]
        assert result.proposal.disposition == DISPOSITION_NEEDS_VERIFICATION
        assert result.fallback_reason == flow.FALLBACK_NO_CHECKABLE_EVIDENCE
        assert result.attempts == 0
        assert result.cost_usd == 0.0
        assert runner.prompts == []

    def test_no_checkable_evidence_never_proposes_punt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(monkeypatch, _StubRunner())
        result = flow.run_triage_proposals(
            _report(checkable=False), _config(tmp_path), record=False
        ).results[0]
        assert result.proposal.disposition != DISPOSITION_PUNT

    def test_empty_backlog_invokes_no_runner_and_spends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode(**kwargs: object) -> object:
            raise AssertionError("no agent may be invoked for an empty backlog")

        monkeypatch.setattr(flow, "run_agent", _explode)
        monkeypatch.setattr(flow, "_select_advisor_profile", _explode)

        summary = flow.run_triage_proposals(
            parse_backlog_report({"findings": []}), _config(tmp_path), record=False
        )
        assert summary.findings_count == 0
        assert summary.total_cost_usd == 0.0


# ── Audit persistence ─────────────────────────────────────────────────────────


class TestAuditPersistence:
    def test_proposals_and_run_spend_are_written_and_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(monkeypatch, _StubRunner(_StubResult(_VALID_PUNT, cost_usd=0.02)))

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=True)
        assert summary.audit_error == ""

        conn = audit_storage.open_readonly(tmp_path)
        try:
            events = list(audit_read_model.iter_triage_proposal_events(conn))
            runs = audit_read_model.triage_proposal_run_spend(conn)
        finally:
            conn.close()

        assert len(events) == 1
        assert events[0]["proposal"]["disposition"] == DISPOSITION_PUNT
        assert events[0]["proposal"]["evidence_refs"] == ["symbol-absent"]
        assert events[0]["cost_usd"] == pytest.approx(0.02)
        assert len(runs) == 1
        assert runs[0]["findings_count"] == 1
        assert runs[0]["total_cost_usd"] == pytest.approx(0.02)
        assert runs[0]["report_path"] == "backlog.json"

    def test_empty_backlog_records_an_explicit_zero_cost_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summary = flow.run_triage_proposals(
            parse_backlog_report({"findings": []}), _config(tmp_path), record=True
        )
        conn = audit_storage.open_readonly(tmp_path)
        try:
            runs = audit_read_model.triage_proposal_run_spend(conn)
        finally:
            conn.close()
        assert len(runs) == 1
        assert runs[0]["findings_count"] == 0
        assert runs[0]["total_cost_usd"] == 0.0
        assert runs[0]["triage_run_id"] == summary.triage_run_id

    def test_validation_errors_survive_into_the_recorded_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(
            monkeypatch,
            _StubRunner(
                _StubResult(_INVALID, cost_usd=0.01), _StubResult(_INVALID, cost_usd=0.01)
            ),
        )
        flow.run_triage_proposals(_report(), _config(tmp_path), record=True)

        conn = audit_storage.open_readonly(tmp_path)
        try:
            rows = conn.execute(
                "SELECT disposition, retry_count, validation_errors FROM triage_proposal_events"
            ).fetchall()
        finally:
            conn.close()
        assert rows[0][0] == DISPOSITION_NEEDS_VERIFICATION
        assert rows[0][1] == 1
        assert "punt_reason_code" in rows[0][2]

    def test_a_prior_disposition_becomes_packet_evidence_for_the_next_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(monkeypatch, _StubRunner(_StubResult(_VALID_PUNT, cost_usd=0.01)))
        flow.run_triage_proposals(_report(), _config(tmp_path), record=True)

        second = _StubRunner(_StubResult(_VALID_PUNT, cost_usd=0.01))
        _install_runner(monkeypatch, second)
        flow.run_triage_proposals(_report(), _config(tmp_path), record=True)

        assert "disposition-history" in second.prompts[0]
        assert "prior disposition row" in second.prompts[0]

    def test_history_lookup_is_scoped_to_the_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(monkeypatch, _StubRunner(_StubResult(_VALID_PUNT, cost_usd=0.01)))
        flow.run_triage_proposals(_report(), _config(tmp_path), record=True)

        conn = audit_storage.open_readonly(tmp_path)
        try:
            mine = audit_read_model.triage_disposition_history(conn, "1310:audit-count")
            other = audit_read_model.triage_disposition_history(conn, "9999:unrelated")
        finally:
            conn.close()
        assert len(mine) == 1
        assert mine[0]["disposition"] == DISPOSITION_PUNT
        assert other == []

    def test_a_substrate_write_failure_is_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(monkeypatch, _StubRunner(_StubResult(_VALID_PUNT, cost_usd=0.01)))

        def _boom(*a: object, **k: object) -> int:
            raise audit_storage.SubstrateError("disk on fire")

        monkeypatch.setattr(audit_storage, "record_triage_proposal_event", _boom)
        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=True)
        assert "disk on fire" in summary.audit_error
        assert summary.results[0].proposal.disposition == DISPOSITION_PUNT


# ── The no-tracker-writes guarantee ───────────────────────────────────────────


class TestNoTrackerWrites:
    """The proposal stage must not be able to touch an issue.

    Both halves matter: no subprocess may be spawned at runtime (which is how
    ``gh`` would be reached), and no tracker-mutating helper may be imported at
    all — a call that is merely never taken on the happy path is one refactor
    away from being taken.
    """

    def test_a_full_run_spawns_no_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(monkeypatch, _StubRunner(_StubResult(_VALID_PUNT, cost_usd=0.01)))

        def _forbidden(*a: object, **k: object) -> None:
            raise AssertionError(f"triage spawned a subprocess: {a!r}")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        monkeypatch.setattr(subprocess, "check_output", _forbidden)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=True)
        assert summary.results[0].proposal.disposition == DISPOSITION_PUNT

    @pytest.mark.parametrize(
        "module_path",
        [
            "src/theforge/coordinator/triage_proposal_flow.py",
            "src/theforge/cli/triage.py",
            "src/theforge/triage_proposal.py",
            "src/theforge/triage_report.py",
            "src/theforge/task/triage_prompts.py",
        ],
    )
    def test_no_triage_module_reaches_a_tracker_write_path(self, module_path: str) -> None:
        source = Path(module_path).read_text(encoding="utf-8")
        # Prose in docstrings names these to say they are absent, so match the
        # call shapes rather than the words.
        forbidden = (
            "github_integration",
            "gh issue",
            "issue_comment",
            "subprocess",
            "_run_shell",
        )
        # Strip the module docstring, which is where the guarantee is stated.
        body = source.split('"""', 2)[-1]
        hits = [token for token in forbidden if token in body]
        assert hits == [], f"{module_path} reaches tracker/subprocess surfaces: {hits}"
