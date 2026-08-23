"""Tests for the ``forge triage`` proposal flow: retries, fallbacks, spend, audit.

The deterministic pipeline is covered here end to end with a stubbed runner:
packet assembly (including the disposition history the substrate holds) →
agent invocation → validation → retry → the ``needs_verification`` fallback →
the audit rows and the reported spend. The schema boundary itself lives in
``test_triage_proposal.py``.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from theforge.agent_types import COST_PROVIDER_REPORTED, COST_UNKNOWN
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    TRIAGE_PROPOSER_TOOLS,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import audit_read_model, audit_storage
from theforge.coordinator import triage_proposal_flow as flow
from theforge.runners.tool_runtime import grants_bash
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

    def __init__(
        self,
        *proposal_results: _StubResult,
        review_results: tuple[_StubResult, ...] | None = None,
    ) -> None:
        self._proposal_results = list(proposal_results)
        self._review_results = list(review_results or [])
        self._default_review_result = (
            None if review_results is not None else _StubResult(_VALID_REVIEW_CONCUR, cost_usd=0.0)
        )
        self.prompts: list[str] = []
        self.review_prompts: list[str] = []

    def __call__(self, **kwargs: object) -> _StubResult:
        prompt = str(kwargs.get("prompt", ""))
        if "ADVERSARIAL PUNT REVIEWER" in prompt:
            self.review_prompts.append(prompt)
            if self._review_results:
                return self._review_results.pop(0)
            if self._default_review_result is not None:
                return self._default_review_result
            raise AssertionError("reviewer invoked more times than scripted")
        self.prompts.append(prompt)
        if not self._proposal_results:
            raise AssertionError("proposer invoked more times than scripted")
        return self._proposal_results.pop(0)


def _install_runner(monkeypatch: pytest.MonkeyPatch, runner: object) -> None:
    monkeypatch.setattr(flow, "run_agent", runner)
    monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_select_advisor_profile", lambda config: DEFAULT_PREFLIGHT_PROFILE)


def _executable_source(path: Path) -> str:
    """Return ``path``'s source with docstrings and comments removed.

    Round-tripping through the AST drops comments outright, and every docstring
    is stripped explicitly, so a static scan sees what the module *does* rather
    than what it says about itself.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def _proposal_block(body: str) -> str:
    return f"<triage_proposal>\n{body}\n</triage_proposal>"


_VALID_PUNT = _proposal_block(
    "disposition: punt\n"
    "punt_reason_code: verified-stale\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
)

_INVALID = _proposal_block(
    "disposition: punt\n"
    "punt_reason_code: feels-old\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
)

# Schema-valid and correctly referenced, but the quote is the proposer's own
# claim rather than the entry's words — rejected by grounding, not by shape.
_UNGROUNDED = _proposal_block(
    "disposition: punt\n"
    "punt_reason_code: verified-stale\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: a maintainer confirmed this was fixed last quarter\n"
)

_VALID_FIX_LATER = _proposal_block(
    "disposition: fix_later\n"
    "target_milestone: Hygiene\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
)


def _review_block(body: str) -> str:
    return f"<triage_punt_review>\n{body}\n</triage_punt_review>"


_VALID_REVIEW_CONCUR = _review_block(
    "verdict: concur\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
)

_VALID_REVIEW_CHALLENGE = _review_block(
    "verdict: challenge\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
)

_INVALID_REVIEW = _review_block(
    "verdict: maybe\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: cited symbol absent from current tree\n"
)

_UNGROUNDED_REVIEW = _review_block(
    "verdict: challenge\n"
    "evidence:\n"
    "  - ref: symbol-absent\n"
    "    quote: a maintainer confirmed this was fixed last quarter\n"
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
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.02),
            review_results=(_StubResult(_VALID_REVIEW_CONCUR, cost_usd=0.03),),
        )
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)

        assert summary.findings_count == 1
        result = summary.results[0]
        assert result.proposal.disposition == DISPOSITION_PUNT
        assert result.proposal.punt_reason_code == "verified-stale"
        assert result.punt_review is not None
        assert result.punt_review.verdict == "concur"
        assert result.cost_usd == pytest.approx(0.02)
        assert result.review_cost_usd == pytest.approx(0.03)
        assert result.retry_count == 0
        assert result.fallback_reason == ""
        assert summary.total_cost_usd == pytest.approx(0.05)

    def test_per_finding_and_total_spend_are_both_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.02),
            _StubResult(_VALID_PUNT, cost_usd=0.03),
            review_results=(
                _StubResult(_VALID_REVIEW_CONCUR, cost_usd=0.04),
                _StubResult(_VALID_REVIEW_CHALLENGE, cost_usd=0.05),
            ),
        )
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(findings=2), _config(tmp_path), record=False)
        assert [r.cost_usd for r in summary.results] == [
            pytest.approx(0.02),
            pytest.approx(0.03),
        ]
        assert [r.review_cost_usd for r in summary.results] == [
            pytest.approx(0.04),
            pytest.approx(0.05),
        ]
        assert summary.total_cost_usd == pytest.approx(0.14)
        assert summary.cost_provenance == COST_PROVIDER_REPORTED

    def test_an_unmeasured_review_attempt_taints_the_total_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.02),
            review_results=(_StubResult(_VALID_REVIEW_CONCUR, cost_usd=None),),
        )
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)
        assert summary.results[0].cost_usd == pytest.approx(0.02)
        assert summary.results[0].review_cost_usd is None
        assert summary.total_cost_usd == pytest.approx(0.02)
        assert summary.cost_provenance == COST_UNKNOWN

    def test_an_unmeasured_proposal_attempt_taints_the_total_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=None),
            review_results=(_StubResult(_VALID_REVIEW_CONCUR, cost_usd=0.03),),
        )
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)
        assert summary.results[0].cost_usd is None
        assert summary.results[0].review_cost_usd == pytest.approx(0.03)
        assert summary.total_cost_usd == pytest.approx(0.03)
        assert summary.cost_provenance == COST_UNKNOWN

    def test_non_punt_proposals_pass_through_unreviewed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(_StubResult(_VALID_FIX_LATER, cost_usd=0.02), review_results=())
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)

        assert summary.results[0].proposal.disposition == "fix_later"
        assert summary.results[0].punt_review is None
        assert summary.review_stage.no_op is True
        assert runner.review_prompts == []


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

    def test_an_ungrounded_claim_is_retried_like_schema_invalid_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real ref with an invented quote gets the same treatment as bad shape."""
        runner = _StubRunner(
            _StubResult(_UNGROUNDED, cost_usd=0.01),
            _StubResult(_VALID_PUNT, cost_usd=0.01),
        )
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.proposal.disposition == DISPOSITION_PUNT
        assert result.retry_count == 1
        assert "not in that entry" in runner.prompts[1]

    def test_a_persistently_ungrounded_claim_becomes_needs_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(
            monkeypatch,
            _StubRunner(
                _StubResult(_UNGROUNDED, cost_usd=0.01), _StubResult(_UNGROUNDED, cost_usd=0.01)
            ),
        )

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.proposal.disposition == DISPOSITION_NEEDS_VERIFICATION
        assert result.fallback_reason == flow.FALLBACK_INVALID_OUTPUT
        assert any("not in that entry" in e for e in result.validation_errors)
        # The unsupported claim is nowhere in what the operator is shown.
        assert "maintainer confirmed" not in result.proposal.evidence

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


class TestPuntReview:
    def test_every_punt_gets_reviewed_before_return(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.01),
            _StubResult(_VALID_PUNT, cost_usd=0.01),
            review_results=(
                _StubResult(_VALID_REVIEW_CONCUR, cost_usd=0.02),
                _StubResult(_VALID_REVIEW_CHALLENGE, cost_usd=0.03),
            ),
        )
        _install_runner(monkeypatch, runner)

        summary = flow.run_triage_proposals(_report(findings=2), _config(tmp_path), record=False)

        verdicts = [result.punt_review.verdict for result in summary.results if result.punt_review]
        assert verdicts == ["concur", "challenge"]
        assert len(runner.review_prompts) == 2
        assert summary.review_stage.reviewed_punt_count == 2
        assert summary.review_stage.challenged_punt_count == 1
        assert summary.review_stage.no_op is False

    def test_invalid_review_is_retried_once_and_can_recover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.01),
            review_results=(
                _StubResult(_INVALID_REVIEW, cost_usd=0.02),
                _StubResult(_VALID_REVIEW_CHALLENGE, cost_usd=0.03),
            ),
        )
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.punt_review is not None
        assert result.punt_review.verdict == "challenge"
        assert result.review_attempts == 2
        assert result.review_retry_count == 1
        assert result.review_cost_usd == pytest.approx(0.05)

    def test_reviewer_retry_prompt_names_the_validator_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.01),
            review_results=(
                _StubResult(_INVALID_REVIEW, cost_usd=0.02),
                _StubResult(_VALID_REVIEW_CONCUR, cost_usd=0.03),
            ),
        )
        _install_runner(monkeypatch, runner)

        flow.run_triage_proposals(_report(), _config(tmp_path), record=False)
        assert "previous attempt was REJECTED" in runner.review_prompts[1]
        assert "verdict must be one of" in runner.review_prompts[1]

    def test_persistently_invalid_review_becomes_a_safe_challenge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.01),
            review_results=(
                _StubResult(_INVALID_REVIEW, cost_usd=0.02),
                _StubResult(_INVALID_REVIEW, cost_usd=0.03),
            ),
        )
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.punt_review is not None
        assert result.punt_review.verdict == "challenge"
        assert result.review_fallback_reason == flow.FALLBACK_REVIEW_INVALID_OUTPUT
        assert any("verdict must be one of" in e for e in result.review_validation_errors)

    def test_ungrounded_review_is_retried_then_challenges_safely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _StubRunner(
            _StubResult(_VALID_PUNT, cost_usd=0.01),
            review_results=(
                _StubResult(_UNGROUNDED_REVIEW, cost_usd=0.02),
                _StubResult(_UNGROUNDED_REVIEW, cost_usd=0.03),
            ),
        )
        _install_runner(monkeypatch, runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert result.punt_review is not None
        assert result.punt_review.verdict == "challenge"
        assert any("not in that entry" in e for e in result.review_validation_errors)

    def test_an_unavailable_reviewer_challenges_rather_than_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompts: list[str] = []

        def _runner(**kwargs: object) -> _StubResult:
            prompt = str(kwargs.get("prompt", ""))
            prompts.append(prompt)
            if "ADVERSARIAL PUNT REVIEWER" in prompt:
                raise RuntimeError("reviewer offline")
            return _StubResult(_VALID_PUNT, cost_usd=0.01)

        _install_runner(monkeypatch, _runner)

        result = flow.run_triage_proposals(_report(), _config(tmp_path), record=False).results[0]
        assert any("ADVERSARIAL PUNT REVIEWER" in prompt for prompt in prompts)
        assert result.punt_review is not None
        assert result.punt_review.verdict == "challenge"
        assert result.review_fallback_reason == flow.FALLBACK_REVIEWER_UNAVAILABLE


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
        assert summary.review_stage.no_op is True


# ── Audit persistence ─────────────────────────────────────────────────────────


class TestAuditPersistence:
    def test_proposals_and_run_spend_are_written_and_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(
            monkeypatch,
            _StubRunner(
                _StubResult(_VALID_PUNT, cost_usd=0.02),
                review_results=(_StubResult(_VALID_REVIEW_CONCUR, cost_usd=0.03),),
            ),
        )

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
        assert events[0]["packet"]["finding_id"] == "1310:audit-count"
        assert events[0]["finding_snapshot"]["issue_ref"] == "#1310"
        assert events[0]["finding_snapshot_digest"]
        assert events[0]["punt_review"]["verdict"] == "concur"
        assert events[0]["cost_usd"] == pytest.approx(0.02)
        assert events[0]["review_cost_usd"] == pytest.approx(0.03)
        assert len(runs) == 1
        assert runs[0]["findings_count"] == 1
        assert runs[0]["total_cost_usd"] == pytest.approx(0.05)
        assert runs[0]["report_path"] == "backlog.json"
        assert runs[0]["review_stage"] == {
            "reviewed_punt_count": 1,
            "challenged_punt_count": 0,
            "no_op": False,
        }

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
        assert runs[0]["review_stage"] == {
            "reviewed_punt_count": 0,
            "challenged_punt_count": 0,
            "no_op": True,
        }

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

    def test_review_fields_survive_into_the_recorded_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_runner(
            monkeypatch,
            _StubRunner(
                _StubResult(_VALID_PUNT, cost_usd=0.01),
                review_results=(
                    _StubResult(_INVALID_REVIEW, cost_usd=0.02),
                    _StubResult(_INVALID_REVIEW, cost_usd=0.03),
                ),
            ),
        )
        flow.run_triage_proposals(_report(), _config(tmp_path), record=True)

        conn = audit_storage.open_readonly(tmp_path)
        try:
            rows = conn.execute(
                "SELECT review_verdict, review_retry_count, review_validation_errors, "
                "review_fallback_reason, review_cost_usd FROM triage_proposal_events"
            ).fetchall()
        finally:
            conn.close()
        assert rows[0][0] == "challenge"
        assert rows[0][1] == 1
        assert "verdict must be one of" in rows[0][2]
        assert rows[0][3] == flow.FALLBACK_REVIEW_INVALID_OUTPUT
        assert rows[0][4] == pytest.approx(0.05)

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

    def test_the_proposer_is_invoked_with_a_sealed_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the runner is actually handed — the mechanical half of the guarantee.

        A prompt that says "do not write anything" is a request. What makes it
        a property of the run is the surface: no shell, a read-only sandbox, a
        working directory that is not the checkout, and no tracker credential.
        """
        captured: dict = {}

        def _capture(**kwargs: object) -> _StubResult:
            captured.update(kwargs)
            # Recorded here, while the scratch directory still exists: the run
            # removes it on the way out.
            captured["contents"] = list(Path(str(kwargs["working_dir"])).iterdir())
            return _StubResult(_VALID_PUNT, cost_usd=0.01)

        monkeypatch.setattr(flow, "run_agent", _capture)
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)
        # A profile that grants everything, so the sealing is what narrows it.
        wide = replace(
            DEFAULT_PREFLIGHT_PROFILE,
            allowed_tools=("Read", "Bash", "Write", "Edit"),
            sandbox_mode="workspace-write",
        )
        monkeypatch.setattr(flow, "_select_advisor_profile", lambda config: wide)

        config = _config(tmp_path)
        object.__setattr__(
            config,
            "secrets",
            {
                "ANTHROPIC_API_KEY": "sk-ant-x",
                "GH_TOKEN": "ghp-secret",
                "GITHUB_TOKEN": "ghp-secret",
                "NTFY_URL": "https://ntfy.sh/topic",
            },
        )

        flow.run_triage_proposals(_report(), config, record=False)

        profile = captured["profile"]
        assert not grants_bash(profile.allowed_tools)
        assert profile.allowed_tools == TRIAGE_PROPOSER_TOOLS
        assert profile.sandbox_mode == "read-only"
        # Not the checkout, and empty: there is nothing here to read or write.
        assert Path(captured["working_dir"]) != tmp_path
        assert captured["contents"] == []
        # Only the provider key survives.
        assert captured["secrets"] == {"ANTHROPIC_API_KEY": "sk-ant-x"}

    def test_the_scratch_directory_is_removed_after_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []

        def _capture(**kwargs: object) -> _StubResult:
            seen.append(Path(str(kwargs["working_dir"])))
            return _StubResult(_VALID_PUNT, cost_usd=0.01)

        monkeypatch.setattr(flow, "run_agent", _capture)
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)
        monkeypatch.setattr(
            flow, "_select_advisor_profile", lambda config: DEFAULT_PREFLIGHT_PROFILE
        )

        flow.run_triage_proposals(_report(), _config(tmp_path), record=False)
        assert seen and not seen[0].exists()

    def test_secret_allowlist_keeps_provider_keys_and_drops_everything_else(self) -> None:
        kept = flow.proposer_secrets(
            {
                "ANTHROPIC_API_KEY": "a",
                "OPENAI_API_KEY": "b",
                "CUSTOM_PROVIDER_API_KEY": "c",
                "GH_TOKEN": "d",
                "GITHUB_TOKEN": "e",
                "NTFY_URL": "f",
                "AWS_SECRET_ACCESS_KEY": "g",
            }
        )
        assert kept == {
            "ANTHROPIC_API_KEY": "a",
            "OPENAI_API_KEY": "b",
            "CUSTOM_PROVIDER_API_KEY": "c",
        }

    def test_sealing_is_applied_through_the_real_runner_binding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercise the production wiring, not a pre-patched module global.

        The other tests replace ``flow.run_agent`` directly, which would keep
        passing if the flow stopped resolving the real runner at all. This one
        clears the lazy slots so ``_ensure_runners`` binds
        ``theforge.runners.run_agent`` itself, and intercepts there.
        """
        import theforge.runners as runners

        captured: dict = {}

        def _capture(**kwargs: object) -> _StubResult:
            captured.update(kwargs)
            return _StubResult(_VALID_PUNT, cost_usd=0.01)

        monkeypatch.setattr(runners, "run_agent", _capture)
        monkeypatch.setattr(runners, "log_agent_result", lambda *a, **k: None)
        monkeypatch.setattr(flow, "run_agent", None)
        monkeypatch.setattr(flow, "log_agent_result", None)
        monkeypatch.setattr(
            flow, "_select_advisor_profile", lambda config: DEFAULT_PREFLIGHT_PROFILE
        )

        summary = flow.run_triage_proposals(_report(), _config(tmp_path), record=False)

        assert summary.results[0].proposal.disposition == DISPOSITION_PUNT
        assert captured["profile"].allowed_tools == TRIAGE_PROPOSER_TOOLS
        assert not grants_bash(captured["profile"].allowed_tools)
        assert Path(captured["working_dir"]) != tmp_path

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
        # Scan executable code only. Docstrings are where these modules *state*
        # the guarantee ("no gh invocation, no GitHub API call"), so a raw text
        # match would fail on the sentence promising the thing it looks for.
        code = _executable_source(Path(module_path))
        forbidden = (
            "github_integration",
            "gh issue",
            "issue_comment",
            "subprocess",
            "_run_shell",
        )
        hits = [token for token in forbidden if token in code]
        assert hits == [], f"{module_path} reaches tracker/subprocess surfaces: {hits}"
