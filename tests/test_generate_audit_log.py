"""Tests for generate_audit_log() — timing, cost.agents, and reviews[].findings."""

from __future__ import annotations

from pathlib import Path

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coord_audit import generate_audit_log
from theforge.coord_state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    ReviewCycleMetadata,
)
from theforge.review import ReviewFinding, ReviewResult
from theforge.runner import AgentResult
from theforge.task import TaskSpec

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
            handoff_file="handoff.yaml",
            gate_decision_key="gate_result",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _make_task(tmp_path: Path) -> TaskSpec:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Test spec", encoding="utf-8")
    return TaskSpec(
        name="Test Task",
        slug="test-task",
        spec_path=spec_path,
        file_scope=[],
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
        spec_matches=True,
        spec_mismatches=[],
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
            description="Bug in error handling",
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
                severity="P1", file="a.py", line=1, description="P1 issue", suggestion=None
            ),
            ReviewFinding(
                severity="P2", file="b.py", line=2, description="P2 issue", suggestion=None
            ),
            ReviewFinding(
                severity="P2", file="c.py", line=3, description="P2 issue 2", suggestion=None
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
            description="Missing docstring",
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
                        severity="P1", file="x.py", line=1, description="issue", suggestion=None
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
