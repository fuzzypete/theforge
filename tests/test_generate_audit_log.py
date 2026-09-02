"""Tests for generate_audit_log() — timing, cost.agents, and reviews[].findings."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.audit_substrate import CURRENT_RECORD_SCHEMA_VERSION
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    DevIterationTelemetry,
    GateDebugTelemetry,
    Phase,
    ReviewCycleMetadata,
)
from theforge.review import ReviewFinding, ReviewResult
from theforge.runners import AgentResult
from theforge.task import TaskStory

# ── Fixtures ──────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(
            gate_command="make gate",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _make_task(tmp_path: Path) -> TaskStory:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Test spec", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        slug="test-task",
        story_path=spec_path,
    )


def _make_agent_result(
    cost_usd: float = 0.10,
    profile_name: str = "dev",
) -> AgentResult:
    return AgentResult(
        success=True,
        output="done",
        session_id=None,
        cost_usd=cost_usd,
        exit_code=0,
        raw={},
        profile_name=profile_name,
    )


def _make_review_result(
    verdict: str = "APPROVE",
    findings: list[ReviewFinding] | None = None,
) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="Looks good",
        findings=findings or [],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _make_coordinator_result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(
        success=True,
        phase=Phase.DONE,
        state=state,
        message="done",
    )


# ── Timing tests ──────────────────────────────────────────────────────


class TestTiming:
    def test_started_at_propagated(self, tmp_path: Path) -> None:
        """started_at from state appears in timing block."""
        state = CoordinatorState()
        state.started_at = "2026-01-01T10:00:00+00:00"
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["timing"]["started_at"] == "2026-01-01T10:00:00+00:00"

    def test_finished_at_present(self, tmp_path: Path) -> None:
        """finished_at is always set (to a non-None string)."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["timing"]["finished_at"] is not None
        assert isinstance(log["timing"]["finished_at"], str)

    def test_duration_seconds_computed(self, tmp_path: Path) -> None:
        """duration_seconds is a positive float when started_at is set."""
        state = CoordinatorState()
        state.started_at = "2020-01-01T00:00:00+00:00"  # far in the past
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        duration = log["timing"]["duration_seconds"]
        assert duration is not None
        assert duration > 0

    def test_duration_seconds_none_when_no_started_at(self, tmp_path: Path) -> None:
        """duration_seconds is None when started_at is not set."""
        state = CoordinatorState()
        assert state.started_at is None
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["timing"]["duration_seconds"] is None


class TestPreflightAudit:
    def test_complexity_routing_assignment_details_are_emitted(self, tmp_path: Path) -> None:
        """Adaptive assignment rationale must appear in the audit log."""
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_reason = "ok"
        state.preflight_complexity = "medium"
        state.preflight_complexity_score = 7
        state.complexity_routing_audit = {
            "source": "adaptive_assignment",
            "adaptive_enabled": True,
            "role_sources": {
                "preflight": "adaptive",
                "planner": "adaptive",
                "plan_review": "adaptive",
                "dev": "explicit_override",
                "code_review": "cap_downgrade",
            },
            "assignments": {
                "planner": {"model": "opus", "source": "builtin"},
                "dev": {"model": "opus", "source": "forge.yaml"},
                "plan_reviewers": [{"model": "sonnet", "source": "builtin"}],
                "code_reviewers": [
                    {"model": "sonnet", "source": "builtin"},
                    {"model": "haiku", "source": "builtin"},
                ],
            },
            "rationale": {
                "dev": "complexity score 7 (MEDIUM) -> tier strong",
                "per_story_routing_cost_target": (
                    "within per-story routing cost target $30.00 (estimated total $27.00)"
                ),
            },
            "per_story_routing_cost_target": {"target_usd": 30.0, "within_target": True},
        }

        log = generate_audit_log(
            _make_config(tmp_path),
            _make_task(tmp_path),
            _make_coordinator_result(state),
        )

        routing = log["preflight"]["complexity_routing"]
        assert routing["source"] == "adaptive_assignment"
        assert routing["adaptive_enabled"] is True
        assert routing["role_sources"]["dev"] == "explicit_override"
        assert routing["role_sources"]["code_review"] == "cap_downgrade"
        assert routing["assignments"]["dev"]["model"] == "opus"
        assert routing["assignments"]["dev"]["source"] == "forge.yaml"
        assert "per_story_routing_cost_target" in routing["rationale"]


# ── Cost.agents tests ─────────────────────────────────────────────────


class TestCostAgents:
    def test_no_agents_when_no_invocations(self, tmp_path: Path) -> None:
        """agents list is empty when no dev or review results exist."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["cost"]["agents"] == []

    def test_dev_agent_entry(self, tmp_path: Path) -> None:
        """A dev invocation produces an agents entry with role=dev."""
        state = CoordinatorState()
        state.dev_results.append(_make_agent_result(cost_usd=0.15, profile_name="dev"))
        state.dev_durations.append(42.5)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        agents = log["cost"]["agents"]
        assert len(agents) == 1
        agent = agents[0]
        assert agent["role"] == "dev"
        assert agent["cost_usd"] == 0.15
        assert agent["duration_seconds"] == 42.5

    def test_review_agent_entry(self, tmp_path: Path) -> None:
        """A review invocation produces an agents entry with role=review."""
        state = CoordinatorState()
        state.review_agent_results.append(_make_agent_result(cost_usd=0.05, profile_name="review"))
        state.review_durations.append(20.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        agents = log["cost"]["agents"]
        assert len(agents) == 1
        agent = agents[0]
        assert agent["role"] == "review"
        assert agent["cost_usd"] == 0.05
        assert agent["duration_seconds"] == 20.0

    def test_multiple_agents_ordering(self, tmp_path: Path) -> None:
        """Dev entries appear before review entries; counts match invocation lists."""
        state = CoordinatorState()
        state.dev_results.append(_make_agent_result(cost_usd=0.10, profile_name="dev"))
        state.dev_results.append(_make_agent_result(cost_usd=0.12, profile_name="dev"))
        state.dev_durations.extend([30.0, 35.0])
        state.review_agent_results.append(_make_agent_result(cost_usd=0.05, profile_name="review"))
        state.review_durations.append(25.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        agents = log["cost"]["agents"]
        assert len(agents) == 3
        assert agents[0]["role"] == "dev"
        assert agents[1]["role"] == "dev"
        assert agents[2]["role"] == "review"

    def test_dev_invocations_count(self, tmp_path: Path) -> None:
        """cost.dev_invocations matches the number of dev results."""
        state = CoordinatorState()
        state.dev_results.append(_make_agent_result())
        state.dev_results.append(_make_agent_result())
        state.dev_durations.extend([10.0, 10.0])
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["cost"]["dev_invocations"] == 2

    def test_review_invocations_count(self, tmp_path: Path) -> None:
        """cost.review_invocations matches the number of review agent results."""
        state = CoordinatorState()
        state.review_agent_results.append(_make_agent_result(profile_name="review"))
        state.review_durations.append(15.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["cost"]["review_invocations"] == 1

    def test_agent_duration_none_when_missing(self, tmp_path: Path) -> None:
        """duration_seconds is None when dev_durations list is shorter than dev_results."""
        state = CoordinatorState()
        state.dev_results.append(_make_agent_result())
        # dev_durations is empty — duration should be None
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["cost"]["agents"][0]["duration_seconds"] is None

    def test_profile_name_in_agent_entry(self, tmp_path: Path) -> None:
        """profile field in agent entry matches the AgentResult profile_name."""
        state = CoordinatorState()
        state.dev_results.append(_make_agent_result(profile_name="sonnet-dev"))
        state.dev_durations.append(10.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["cost"]["agents"][0]["profile"] == "sonnet-dev"


# ── Reviews findings tests ─────────────────────────────────────────────


class TestReviewsFindings:
    def test_no_reviews_when_no_cycles(self, tmp_path: Path) -> None:
        """reviews list is empty when no review cycles ran."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["reviews"] == []

    def test_findings_list_in_review_entry(self, tmp_path: Path) -> None:
        """reviews[].findings contains severity/file/line/description for each finding."""
        finding = ReviewFinding(
            severity="P1",
            file="src/foo.py",
            line=42,
            observed="Bug in error handling",
            suggestion="Fix it",
        )
        state = CoordinatorState()
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["review"],
                successful=["review"],
                failed=[],
                synthesized=False,
            )
        )
        state.review_results.append(
            _make_review_result(verdict="REQUEST_CHANGES", findings=[finding])
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        reviews = log["reviews"]
        assert len(reviews) == 1
        assert "findings" in reviews[0]
        assert len(reviews[0]["findings"]) == 1
        f = reviews[0]["findings"][0]
        assert f["severity"] == "P1"
        assert f["file"] == "src/foo.py"
        assert f["line"] == 42
        assert f["description"] == "Bug in error handling"

    def test_p1_p2_counts_still_present(self, tmp_path: Path) -> None:
        """p1_count and p2_count are still included alongside findings list."""
        findings = [
            ReviewFinding(
                severity="P1", file="a.py", line=1, observed="P1 issue", suggestion=None
            ),
            ReviewFinding(
                severity="P2", file="b.py", line=2, observed="P2 issue", suggestion=None
            ),
            ReviewFinding(
                severity="P2", file="c.py", line=3, observed="P2 issue 2", suggestion=None
            ),
        ]
        state = CoordinatorState()
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["review"],
                successful=["review"],
                failed=[],
                synthesized=False,
            )
        )
        state.review_results.append(
            _make_review_result(verdict="REQUEST_CHANGES", findings=findings)
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        entry = log["reviews"][0]
        assert entry["p1_count"] == 1
        assert entry["p2_count"] == 2
        assert len(entry["findings"]) == 3

    def test_empty_findings_list(self, tmp_path: Path) -> None:
        """reviews[].findings is an empty list when verdict is APPROVE with no findings."""
        state = CoordinatorState()
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["review"],
                successful=["review"],
                failed=[],
                synthesized=False,
            )
        )
        state.review_results.append(_make_review_result(verdict="APPROVE", findings=[]))
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["reviews"][0]["findings"] == []
        assert log["reviews"][0]["p1_count"] == 0
        assert log["reviews"][0]["p2_count"] == 0

    def test_finding_with_null_line(self, tmp_path: Path) -> None:
        """findings entry handles line=None without error."""
        finding = ReviewFinding(
            severity="P2",
            file="src/bar.py",
            line=None,
            observed="Missing docstring",
            suggestion=None,
        )
        state = CoordinatorState()
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["review"],
                successful=["review"],
                failed=[],
                synthesized=False,
            )
        )
        state.review_results.append(
            _make_review_result(verdict="REQUEST_CHANGES", findings=[finding])
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        f = log["reviews"][0]["findings"][0]
        assert f["line"] is None
        assert f["severity"] == "P2"

    def test_multiple_review_cycles(self, tmp_path: Path) -> None:
        """Multiple cycles produce correctly indexed reviews list."""
        state = CoordinatorState()
        for i in range(2):
            state.review_cycle_metadata.append(
                ReviewCycleMetadata(
                    pool_models=["review"],
                    successful=["review"],
                    failed=[],
                    synthesized=False,
                )
            )
        state.review_results.append(
            _make_review_result(
                verdict="REQUEST_CHANGES",
                findings=[
                    ReviewFinding(
                        severity="P1", file="x.py", line=1, observed="issue", suggestion=None
                    )
                ],
            )
        )
        state.review_results.append(_make_review_result(verdict="APPROVE", findings=[]))
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        reviews = log["reviews"]
        assert len(reviews) == 2
        assert reviews[0]["cycle"] == 1
        assert reviews[0]["verdict"] == "REQUEST_CHANGES"
        assert len(reviews[0]["findings"]) == 1
        assert reviews[1]["cycle"] == 2
        assert reviews[1]["verdict"] == "APPROVE"
        assert reviews[1]["findings"] == []


# ── Phases block tests ─────────────────────────────────────────────────


class TestPhasesBlock:
    def test_phases_key_present(self, tmp_path: Path) -> None:
        """generate_audit_log always includes phases and totals keys."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert "phases" in log
        assert "totals" in log

    def test_preflight_phase_populated(self, tmp_path: Path) -> None:
        """phases.preflight is populated when preflight ran."""
        state = CoordinatorState()
        state.preflight_result = _make_agent_result(cost_usd=0.11)
        state.preflight_verdict = "PROCEED"
        state.preflight_duration_s = 31.0
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        pf = log["phases"]["preflight"]
        assert pf is not None
        assert pf["cost_usd"] == pytest.approx(0.11)
        assert pf["duration_s"] == pytest.approx(31.0)
        assert pf["outcome"] == "proceed"

    def test_preflight_phase_none_when_not_run(self, tmp_path: Path) -> None:
        """phases.preflight is None when preflight was not run."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["phases"]["preflight"] is None

    def test_plan_phase_populated(self, tmp_path: Path) -> None:
        """phases.plan is populated from plan_results and plan_durations."""
        state = CoordinatorState()
        state.plan_results.append(_make_agent_result(cost_usd=0.21))
        state.plan_durations.append(78.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        plan = log["phases"]["plan"]
        assert plan is not None
        assert plan["cost_usd"] == pytest.approx(0.21)
        assert plan["duration_s"] == pytest.approx(78.0)

    def test_plan_phase_none_when_not_run(self, tmp_path: Path) -> None:
        """phases.plan is None when plan phase was not run."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["phases"]["plan"] is None

    def test_plan_review_phase_populated(self, tmp_path: Path) -> None:
        """phases.plan_review is populated when plan review ran."""
        state = CoordinatorState()
        state.plan_review_results.append(_make_agent_result(cost_usd=0.53))
        state.plan_review_durations.append(73.0)
        state.plan_review_decision = "approve"
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        pr = log["phases"]["plan_review"]
        assert pr is not None
        assert pr["cost_usd"] == pytest.approx(0.53)
        assert pr["duration_s"] == pytest.approx(73.0)
        assert pr["outcome"] == "approve"

    def test_plan_review_phase_includes_per_reviewer_outcomes(self, tmp_path: Path) -> None:
        """Agent plan review emits attempt-tagged per_reviewer outcomes in the phase summary."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            plan_agent_review=PlanAgentReviewConfig.of(
                enabled=True,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        cli="claude",
                        model="sonnet",
                        budget_usd=1.0,
                        timeout_seconds=300,
                        allowed_tools=("Read",),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        cli="codex",
                        model="gpt-5",
                        budget_usd=1.0,
                        timeout_seconds=300,
                        allowed_tools=("Read",),
                    ),
                ],
            ),
        )
        state = CoordinatorState()
        state.plan_review_mode = "agent"
        state.plan_review_decision = "approve"
        state.plan_review_durations.extend([30.0, 40.0])
        state.plan_review_results.extend(
            [
                AgentResult(
                    success=True,
                    output=(
                        "verdict: REJECT\n"
                        "findings:\n"
                        "  - severity: P1\n"
                        "    description: Missing rollback path\n"
                    ),
                    session_id=None,
                    cost_usd=0.21,
                    exit_code=0,
                    raw={},
                    profile_name="reviewer-a",
                ),
                AgentResult(
                    success=True,
                    output="not valid yaml: [",
                    session_id=None,
                    cost_usd=0.02,
                    exit_code=0,
                    raw={},
                    profile_name="reviewer-b",
                ),
                AgentResult(
                    success=True,
                    output="verdict: APPROVE\nfindings: []\n",
                    session_id=None,
                    cost_usd=0.19,
                    exit_code=0,
                    raw={},
                    profile_name="reviewer-a",
                ),
                AgentResult(
                    success=False,
                    output="provider crash",
                    session_id=None,
                    cost_usd=0.0,
                    exit_code=1,
                    raw={},
                    profile_name="reviewer-b",
                    failure_code="provider_5xx",
                ),
            ]
        )
        result = _make_coordinator_result(state)

        log = generate_audit_log(config, _make_task(tmp_path), result)

        pr = log["phases"]["plan_review"]
        assert pr is not None
        assert pr["per_reviewer"] == [
            {
                "attempt": 0,
                "profile": "reviewer-a",
                "verdict": "REQUEST_CHANGES",
                "cost_usd": pytest.approx(0.21),
            },
            {
                "attempt": 0,
                "profile": "reviewer-b",
                "verdict": "PARSE_ERROR",
                "cost_usd": pytest.approx(0.02),
            },
            {
                "attempt": 1,
                "profile": "reviewer-a",
                "verdict": "APPROVE",
                "cost_usd": pytest.approx(0.19),
            },
            {
                "attempt": 1,
                "profile": "reviewer-b",
                "verdict": "CRASHED",
                "cost_usd": pytest.approx(0.0),
                "crash_kind": "provider_5xx",
            },
        ]

    def test_dev_phase_populated(self, tmp_path: Path) -> None:
        """phases.dev is populated from dev_results and dev_durations."""
        state = CoordinatorState()
        state.dev_results.append(_make_agent_result(cost_usd=3.26))
        state.dev_durations.append(969.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        dev = log["phases"]["dev"]
        assert dev is not None
        assert dev["cost_usd"] == pytest.approx(3.26)
        assert dev["duration_s"] == pytest.approx(969.0)
        assert dev["iterations"] == 1

    def test_dev_phase_includes_transport_retries(self, tmp_path: Path) -> None:
        """Transient dev retries appear in both phase and iteration audit blocks."""
        state = CoordinatorState()
        state.dev_results.extend(
            [
                AgentResult(
                    success=False,
                    output="API Error: Stream idle timeout - partial response received",
                    session_id="sess-partial",
                    cost_usd=0.75,
                    exit_code=1,
                    raw={},
                    profile_name="dev",
                ),
                _make_agent_result(cost_usd=1.10, profile_name="dev"),
            ]
        )
        state.dev_durations.extend([120.0, 240.0])
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=1,
                max_iterations=3,
                cost_usd=1.10,
                duration_s=360.0,
                gate_result="PASS",
                transport_retry_count=1,
                transport_retry_events=[
                    {
                        "iteration": 1,
                        "retry": 1,
                        "error": (
                            "exit=1: API Error: Stream idle timeout - partial response received"
                        ),
                    }
                ],
            )
        )
        result = _make_coordinator_result(state)

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        dev = log["phases"]["dev"]
        assert dev is not None
        assert len(dev["transport_retries"]) == 1
        assert dev["transport_retries"][0]["retry"] == 1
        assert log["iterations"]["dev_loop"][0]["transport_retry_count"] == 1

    def test_validate_phase_populated(self, tmp_path: Path) -> None:
        """phases.validate is populated when gate ran."""
        state = CoordinatorState()
        state.gate_decisions.append("PASS")
        state.validate_durations.append(12.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        validate = log["phases"]["validate"]
        assert validate is not None
        assert validate["duration_s"] == pytest.approx(12.0)
        assert validate["outcome"] == "pass"

    def test_validate_phase_records_gate_debug_timeout_audit(self, tmp_path: Path) -> None:
        """Gate timeout diagnostics appear in validate phase and iteration audit."""
        state = CoordinatorState()
        state.validate_durations.append(45.0)
        state.gate_debug_telemetry.append(
            GateDebugTelemetry(
                trace_index=1,
                trace_path=".forge/traces/1-gate-debug.txt",
                command="pytest -x -v -n 0",
                ran=True,
                timeout_s=45,
                exit_code=5,
                output_tail="debug tail",
                output_truncated=True,
            )
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        validate = log["phases"]["validate"]
        assert validate is not None
        assert validate["outcome"] == "error"
        assert validate["gate_debug"][0]["ran"] is True
        assert validate["gate_debug"][0]["exit_code"] == 5
        assert validate["gate_debug"][0]["output_tail"] == "debug tail"
        assert log["iterations"]["gate_debug"][0]["command"] == "pytest -x -v -n 0"

    def test_review_phase_per_reviewer(self, tmp_path: Path) -> None:
        """phases.review.per_reviewer groups agents by profile, cross-refs verdict."""
        state = CoordinatorState()
        state.review_agent_results.append(
            _make_agent_result(cost_usd=0.50, profile_name="claude-reviewer")
        )
        state.review_agent_results.append(
            _make_agent_result(cost_usd=0.45, profile_name="codex-reviewer")
        )
        state.review_durations.extend([100.0, 108.0])
        state.review_cycle = 1
        state.last_cycle_reviewer_results = [
            ("claude-reviewer", _make_review_result("APPROVE")),
            ("codex-reviewer", _make_review_result("APPROVE")),
        ]
        state.review_results.append(_make_review_result("APPROVE"))
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["claude-reviewer", "codex-reviewer"],
                successful=["claude-reviewer", "codex-reviewer"],
                failed=[],
                synthesized=False,
            )
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        review = log["phases"]["review"]
        assert review is not None
        per = review["per_reviewer"]
        assert "claude-reviewer" in per
        assert "codex-reviewer" in per
        assert per["claude-reviewer"]["cost"] == pytest.approx(0.50)
        assert per["claude-reviewer"]["verdict"] == "APPROVE"
        assert per["codex-reviewer"]["cost"] == pytest.approx(0.45)

    def test_synthesis_excluded_from_per_reviewer(self, tmp_path: Path) -> None:
        """synthesis agent is excluded from phases.review.per_reviewer."""
        state = CoordinatorState()
        state.review_agent_results.append(
            _make_agent_result(cost_usd=0.50, profile_name="claude-reviewer")
        )
        state.review_agent_results.append(
            _make_agent_result(cost_usd=0.02, profile_name="synthesis")
        )
        state.review_durations.extend([100.0, 5.0])
        state.review_cycle = 1
        state.last_cycle_reviewer_results = [
            ("claude-reviewer", _make_review_result("APPROVE")),
        ]
        state.review_results.append(_make_review_result("APPROVE"))
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["claude-reviewer"],
                successful=["claude-reviewer"],
                failed=[],
                synthesized=True,
            )
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        per = log["phases"]["review"]["per_reviewer"]
        assert "synthesis" not in per
        assert "claude-reviewer" in per

    def test_totals_cost_equals_state_total_cost(self, tmp_path: Path) -> None:
        """totals.cost_usd equals state.total_cost."""
        state = CoordinatorState()
        state.dev_results.append(_make_agent_result(cost_usd=3.26))
        state.review_agent_results.append(
            _make_agent_result(cost_usd=1.77, profile_name="reviewer")
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["totals"]["cost_usd"] == pytest.approx(state.total_cost)

    def test_all_phase_keys_present(self, tmp_path: Path) -> None:
        """phases dict always has all six phase keys, even if their values are None."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        for key in ("preflight", "plan", "plan_review", "dev", "validate", "review"):
            assert key in log["phases"], f"missing phase key: {key}"


def test_generate_audit_log_includes_context_manifests(tmp_path: Path) -> None:
    state = CoordinatorState()
    from theforge.task import ContextManifestEntry, ContextPack

    pack = ContextPack(
        content="ctx",
        included=(
            ContextManifestEntry(
                source="src/a.py",
                kind="claude_invariants",
                required=True,
                lines=2,
                included=True,
                reason="invariants",
                item_type="invariant",
            ),
        ),
        dropped=(
            ContextManifestEntry(
                source="src/b.py",
                kind="claude_advisory",
                required=False,
                lines=3,
                included=False,
                reason="advisory",
                item_type="advisory",
                drop_reason="out of scope",
            ),
        ),
        budget=10,
        line_count=2,
        phase="dev",
        structural_index_git_sha="abc123",
    )
    state.context_manifests.append({"phase": "dev", "manifest": pack})
    result = _make_coordinator_result(state)
    log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

    manifest = log["context_manifests"][0]
    assert manifest["phase"] == "dev"
    assert manifest["context_budget"] == 10
    assert manifest["git_sha"] == "abc123"
    assert manifest["items_included"][0]["type"] == "invariant"
    assert manifest["items_dropped"][0]["reason"] == "out of scope"
    # A pack built without prior-run knowledge still reports the gate as off,
    # so "nothing was injected" is an audited fact rather than a missing key.
    assert manifest["prior_run_context"]["enabled"] is False
    assert manifest["prior_run_context"]["index_state"] is None


def test_generate_audit_log_includes_prior_run_context_decisions(tmp_path: Path) -> None:
    state = CoordinatorState()
    from theforge.task import ContextManifestEntry, ContextPack

    pack = ContextPack(
        content="ctx",
        included=(
            ContextManifestEntry(
                source="knowledge:4f2a91c",
                kind="prior_run_summary",
                required=False,
                lines=6,
                included=True,
                reason="file_overlap(sprint/runner.py), domain_match(sprint)",
                score=16,
                item_type="advisory",
            ),
        ),
        dropped=(
            ContextManifestEntry(
                source="knowledge:9c11e0a",
                kind="prior_run_summary",
                required=False,
                lines=6,
                included=False,
                reason="file_overlap(sprint/runner.py)",
                item_type="advisory",
                drop_reason="budget_pressure",
            ),
        ),
        budget=10,
        line_count=6,
        phase="dev",
        structural_index_git_sha=None,
        prior_run_context={
            "enabled": True,
            "index_state": "ready",
            "included": [
                {
                    "run_id": "4f2a91c",
                    "reason": "file_overlap(sprint/runner.py), domain_match(sprint)",
                    "score": 16,
                    "rendered_size": {
                        "value": 42,
                        "unit": "tokens",
                        "method": "word_punctuation_estimate_v1",
                        "kind": "rendered_prompt_contribution",
                    },
                    "verdict": {"status": "admissible", "rank": "full"},
                }
            ],
            "dropped": [
                {"run_id": "9c11e0a", "reason": "budget_pressure"},
                {
                    "run_id": "71bd334",
                    "reason": "inadmissible(cited_source_deleted)",
                    "verdict": {
                        "status": "inadmissible",
                        "rank": "excluded",
                        "reasons": ["cited_source_deleted"],
                    },
                },
            ],
            "note": (
                "1 prior summaries included; "
                "1 summaries matched but were excluded on admissibility"
            ),
        },
    )
    state.context_manifests.append({"phase": "dev", "manifest": pack})
    result = _make_coordinator_result(state)
    log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

    prior = log["context_manifests"][0]["prior_run_context"]
    assert prior["enabled"] is True
    assert prior["included"][0]["run_id"] == "4f2a91c"
    assert prior["included"][0]["rendered_size"]["value"] == 42
    assert prior["included"][0]["verdict"]["status"] == "admissible"
    dropped = {item["run_id"]: item for item in prior["dropped"]}
    assert dropped["9c11e0a"]["reason"] == "budget_pressure"
    assert dropped["71bd334"]["verdict"]["reasons"] == ["cited_source_deleted"]
    assert "excluded on admissibility" in prior["note"]
    assert log["context_manifests"][0]["items_dropped"][0]["reason"] == "budget_pressure"


# ── Layer 1 knowledge capture tests ──────────────────────────────────────────


class TestKnowledgeCaptureLayer1:
    """Tests for Layer 1 audit enrichment: run_id, story_text, github_issue,
    story_path null fix, schema_version, plan_structured, attempt_plans."""

    def test_schema_version_present(self, tmp_path: Path) -> None:
        """schema_version is always emitted in the audit record."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["schema_version"] == CURRENT_RECORD_SCHEMA_VERSION

    def test_run_id_from_state(self, tmp_path: Path) -> None:
        """run_id in audit record comes from state.run_id."""
        state = CoordinatorState()
        state.run_id = "abc123def456"
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["run_id"] == "abc123def456"

    def test_run_id_none_when_not_set(self, tmp_path: Path) -> None:
        """run_id is None when state.run_id was never set (backwards compat)."""
        state = CoordinatorState()
        assert state.run_id is None
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["run_id"] is None

    def test_story_text_from_state(self, tmp_path: Path) -> None:
        """task.story_text in audit comes from state.story_content."""
        state = CoordinatorState()
        state.story_content = "## Story\n\nDo the thing."
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["task"]["story_text"] == "## Story\n\nDo the thing."

    def test_story_text_none_when_not_set(self, tmp_path: Path) -> None:
        """task.story_text is None when state.story_content is not set."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["task"]["story_text"] is None

    def test_github_issue_propagated(self, tmp_path: Path) -> None:
        """task.github_issue from TaskStory appears in audit task block."""
        state = CoordinatorState()
        task = TaskStory(name="Test", slug="test", github_issue=42)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), task, result)

        assert log["task"]["github_issue"] == 42

    def test_github_issue_none(self, tmp_path: Path) -> None:
        """task.github_issue is None when no issue is linked."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["task"]["github_issue"] is None

    def test_story_path_null_for_issue_sourced(self, tmp_path: Path) -> None:
        """story_path serializes as None (not the string 'None') for issue-sourced tasks."""
        state = CoordinatorState()
        task = TaskStory(name="Test", slug="test", story_path=None)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), task, result)

        assert log["task"]["story_path"] is None
        assert log["task"]["story_path"] != "None"

    def test_story_path_string_when_present(self, tmp_path: Path) -> None:
        """story_path serializes as a string when a path is set."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert isinstance(log["task"]["story_path"], str)
        assert log["task"]["story_path"] != "None"

    def test_plan_structured_in_plan_block(self, tmp_path: Path) -> None:
        """phases.plan.plan_structured contains the final parsed plan."""
        state = CoordinatorState()
        state.plan_results.append(_make_agent_result(cost_usd=0.10))
        state.plan_durations.append(30.0)
        state.plan_structured = {
            "approach": "Refactor incrementally",
            "steps": [
                {
                    "id": 1,
                    "description": "Update types",
                    "files": ["src/types.py"],
                    "action": "modify",
                    "details": "Add new fields",
                }
            ],
        }
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        plan = log["phases"]["plan"]
        assert plan is not None
        assert plan["plan_structured"] is not None
        assert plan["plan_structured"]["approach"] == "Refactor incrementally"
        assert len(plan["plan_structured"]["steps"]) == 1

    def test_plan_structured_none_when_fallback(self, tmp_path: Path) -> None:
        """phases.plan.plan_structured is None when plan fell back to markdown."""
        state = CoordinatorState()
        state.plan_results.append(_make_agent_result(cost_usd=0.10))
        state.plan_durations.append(30.0)
        state.plan_structured = None
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["phases"]["plan"]["plan_structured"] is None

    def test_attempt_plans_empty_when_no_regen(self, tmp_path: Path) -> None:
        """phases.plan.attempt_plans is empty when no plan regeneration occurred."""
        state = CoordinatorState()
        state.plan_results.append(_make_agent_result(cost_usd=0.10))
        state.plan_durations.append(30.0)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["phases"]["plan"]["attempt_plans"] == []

    def test_attempt_plans_lineage_preserved(self, tmp_path: Path) -> None:
        """phases.plan.attempt_plans records prior plans with attempt numbers on regen."""
        initial_plan = {
            "approach": "Initial approach",
            "steps": [],
        }
        state = CoordinatorState()
        state.plan_results.append(_make_agent_result(cost_usd=0.10))
        state.plan_durations.append(30.0)
        state.plan_attempt_plans.append(initial_plan)  # simulates snapshot before regen
        state.plan_structured = {
            "approach": "Revised approach",
            "steps": [],
        }
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        attempts = log["phases"]["plan"]["attempt_plans"]
        assert len(attempts) == 1
        assert attempts[0]["attempt"] == 0
        assert attempts[0]["plan"]["approach"] == "Initial approach"
        # Final plan is in plan_structured, not attempt_plans
        assert log["phases"]["plan"]["plan_structured"]["approach"] == "Revised approach"

    def test_attempt_plans_multiple_regens(self, tmp_path: Path) -> None:
        """Multiple regen cycles produce correctly indexed attempt entries."""
        state = CoordinatorState()
        state.plan_results.append(_make_agent_result(cost_usd=0.30))
        state.plan_durations.append(90.0)
        state.plan_attempt_plans.append({"approach": "Attempt 0", "steps": []})
        state.plan_attempt_plans.append({"approach": "Attempt 1", "steps": []})
        state.plan_structured = {"approach": "Final (attempt 2)", "steps": []}
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        attempts = log["phases"]["plan"]["attempt_plans"]
        assert len(attempts) == 2
        assert attempts[0]["attempt"] == 0
        assert attempts[0]["plan"]["approach"] == "Attempt 0"
        assert attempts[1]["attempt"] == 1
        assert attempts[1]["plan"]["approach"] == "Attempt 1"

    def test_attempt_plans_none_when_plan_not_run(self, tmp_path: Path) -> None:
        """phases.plan is None when plan phase never ran (no attempt_plans key)."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["phases"]["plan"] is None

    def test_fix_ready_propagated(self, tmp_path: Path) -> None:
        """task.fix_ready from TaskStory appears in audit task block."""
        state = CoordinatorState()
        task = TaskStory(name="Test", slug="test", fix_ready=True)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), task, result)

        assert log["task"]["fix_ready"] is True

    def test_fix_ready_false_propagated(self, tmp_path: Path) -> None:
        """task.fix_ready=False (shape gate rejection) is preserved, not dropped as falsy."""
        state = CoordinatorState()
        task = TaskStory(name="Test", slug="test", fix_ready=False)
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), task, result)

        assert log["task"]["fix_ready"] is False

    def test_fix_ready_none_when_not_set(self, tmp_path: Path) -> None:
        """task.fix_ready is None when the story is not a bug (default unset)."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["task"]["fix_ready"] is None

    def test_readiness_warnings_propagated(self, tmp_path: Path) -> None:
        """task.readiness_warnings from TaskStory appears in audit task block."""
        state = CoordinatorState()
        task = TaskStory(
            name="Test",
            slug="test",
            fix_ready=False,
            readiness_warnings=["missing Diagnosis section", "no confirmed cause"],
        )
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), task, result)

        assert log["task"]["readiness_warnings"] == [
            "missing Diagnosis section",
            "no confirmed cause",
        ]

    def test_readiness_warnings_empty_list_when_not_set(self, tmp_path: Path) -> None:
        """task.readiness_warnings defaults to an empty list, not None."""
        state = CoordinatorState()
        result = _make_coordinator_result(state)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)

        assert log["task"]["readiness_warnings"] == []
