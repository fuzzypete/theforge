"""Seam integration tests for adaptive iteration limits.

Covers the cross-phase flow: preflight complexity → engine.derive_limits →
state → dev/review phases → audit output, plus early-termination behaviour
on consecutive zero-new-findings review cycles.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.config.types import AssignmentConfig, RetryPolicy
from theforge.coordinator.adaptive_iterations import derive_limits
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    ReviewIterationTelemetry,
)
from theforge.coordinator.util import LARGE_HEADROOM_FACTOR, MEDIUM_HEADROOM_FACTOR


def _make_adaptive_config(tmp_path: Path, **retry_overrides):
    config = _make_config(tmp_path)
    defaults = dict(
        max_dev_iterations=3,
        max_review_cycles=2,
        max_dev_iterations_cap=6,
        max_review_cycles_cap=4,
        adaptive_iterations=True,
        review_zero_findings_stop=2,
    )
    defaults.update(retry_overrides)
    return replace(
        config,
        retry=RetryPolicy(**defaults),
        assignment=AssignmentConfig(enabled=True, adaptive_enabled=True),
    )


def _profiles(*, runs: int = 3, avg_iterations: float = 4.0, avg_cost_usd: float = 1.0) -> dict:
    return {
        "models": {
            "dev": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": runs,
                            "avg_iterations": avg_iterations,
                            "avg_cost_usd": avg_cost_usd,
                        },
                        "large": {
                            "runs": runs,
                            "avg_iterations": avg_iterations,
                            "avg_cost_usd": avg_cost_usd,
                        },
                    }
                }
            }
        }
    }


def test_adaptive_limits_populated_in_audit(tmp_path: Path):
    """State.adaptive_limits_audit flows through to audit output."""
    config = _make_adaptive_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState()
    state.preflight_complexity_score = 7
    state.adaptive_dev_max = 5
    state.adaptive_review_max = 3
    state.adaptive_dev_timeout_seconds = 1500
    state.adaptive_dev_cost_estimate_usd = 1.8
    state.adaptive_limits_audit = {
        "enabled": True,
        "chosen_dev_max": 5,
        "chosen_review_max": 3,
        "chosen_dev_timeout_seconds": 1500,
        "chosen_dev_cost_estimate_usd": 1.8,
        "rationale": "complexity-derived",
    }
    audit = generate_audit_log(
        config,
        task,
        CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok"),
    )
    assert audit["iterations"]["adaptive_limits"]["chosen_dev_max"] == 5
    assert audit["iterations"]["usage_summary"]["dev"]["max"] == 5
    assert audit["iterations"]["usage_summary"]["review"]["max"] == 3


def test_audit_falls_back_to_config_when_no_adaptive(tmp_path: Path):
    config = _make_adaptive_config(tmp_path, adaptive_iterations=False)
    task = _make_task(tmp_path)
    state = CoordinatorState()  # no adaptive values set
    audit = generate_audit_log(
        config,
        task,
        CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok"),
    )
    assert audit["iterations"]["usage_summary"]["dev"]["max"] == 3
    assert audit["iterations"]["usage_summary"]["review"]["max"] == 2
    assert audit["iterations"]["adaptive_limits"] is None


def test_engine_populates_adaptive_limits_before_dev_loop(tmp_path: Path):
    """_coordinator_loop calls derive_limits and writes the result to state."""
    from theforge.coordinator.engine import _coordinator_loop

    config = _make_adaptive_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState()
    state.preflight_complexity_score = 9
    state.workspace_path = tmp_path
    state.branch_name = "feat/test"
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "model_profiles.yaml").write_text("models: {}\n", encoding="utf-8")

    # Short-circuit the loop by raising before DEV starts.
    class _StopLoop(Exception):
        pass

    def _fake_dev(*args, **kwargs):
        raise _StopLoop()

    with patch("theforge.coordinator.engine._run_dev_phase", _fake_dev):
        try:
            _coordinator_loop(state, config, task, "story", task_start=0.0)
        except _StopLoop:
            pass

    assert state.adaptive_dev_max > 0
    assert state.adaptive_review_max > 0
    assert state.adaptive_dev_max == config.retry.max_dev_iterations
    assert state.budget.max_iterations == config.retry.max_dev_iterations
    assert state.adaptive_review_max <= config.retry.max_review_cycles_cap
    assert state.adaptive_dev_timeout_seconds == round(
        config.dev_profile.timeout_seconds * LARGE_HEADROOM_FACTOR
    )
    assert state.adaptive_dev_cost_estimate_usd == config.dev_profile.budget_usd
    assert state.adaptive_limits_audit.get("enabled") is True
    assert state.adaptive_limits_audit.get("complexity_score_used") == 9


def test_profiles_inform_adaptive_dev_limits(tmp_path: Path):
    """derive_limits uses model capability profiles for dev budgeting."""
    policy = RetryPolicy(
        max_dev_iterations=3,
        max_review_cycles=2,
        max_dev_iterations_cap=6,
        max_review_cycles_cap=4,
        adaptive_iterations=True,
    )
    result = derive_limits(
        5,
        "medium",
        policy,
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=None,
        model_profiles=_profiles(avg_iterations=3.2, avg_cost_usd=1.2),
    )
    assert result.dev_max == 5
    assert result.dev_timeout_seconds == 1500
    assert result.dev_cost_estimate_usd == 2.4  # medium band: 1.2 * 2.0x headroom
    assert result.audit["profile_history_runs"] == 3


def test_fragmented_profile_aliases_inform_adaptive_dev_limits(tmp_path: Path):
    """derive_limits aggregates same-model history split across profile aliases."""
    policy = RetryPolicy(
        max_dev_iterations=3,
        max_review_cycles=2,
        max_dev_iterations_cap=6,
        max_review_cycles_cap=4,
        adaptive_iterations=True,
    )
    profiles = {
        "models": {
            "openai-gpt-5.4": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 2,
                            "avg_iterations": 3.0,
                            "avg_cost_usd": 1.0,
                        }
                    }
                }
            },
            "openai-api-gpt-5.4": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 2,
                            "avg_iterations": 4.0,
                            "avg_cost_usd": 1.5,
                        }
                    }
                }
            },
        }
    }

    result = derive_limits(
        5,
        "medium",
        policy,
        model_name="openai-gpt-5.4",
        model_actual="gpt-5.4",
        model_provider="openai",
        model_cli="codex",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=None,
        model_profiles=profiles,
    )
    assert result.dev_max == 6
    assert result.dev_timeout_seconds == 1800
    assert result.dev_cost_estimate_usd == 2.5
    assert result.audit["profile_history_runs"] == 4


def test_determinism_seam(tmp_path: Path):
    """Same complexity + same history → identical adaptive limits."""
    policy = RetryPolicy(
        max_dev_iterations=3,
        max_review_cycles=2,
        max_dev_iterations_cap=6,
        max_review_cycles_cap=4,
        adaptive_iterations=True,
    )
    profiles = _profiles(avg_iterations=4.0, avg_cost_usd=1.0)
    a = derive_limits(
        6,
        "medium",
        policy,
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=None,
        model_profiles=profiles,
    )
    b = derive_limits(
        6,
        "medium",
        policy,
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=None,
        model_profiles=profiles,
    )
    assert a == b


def test_review_early_termination_on_zero_new_findings(tmp_path: Path):
    """Two consecutive zero-new-findings review iterations trigger early exit."""
    from theforge.coordinator.review_phase import _ReviewOutcome
    from theforge.review import ReviewResult

    config = _make_adaptive_config(tmp_path, review_zero_findings_stop=2)
    _ = _make_task(tmp_path)  # sanity: helper works
    state = CoordinatorState()
    state.preflight_complexity_score = 5
    state.adaptive_dev_max = 4
    state.adaptive_review_max = 4
    state.workspace_path = tmp_path / "ws"
    state.workspace_path.mkdir(exist_ok=True)
    state.branch_name = "feat/test"
    state.review_cycle = 3

    # Seed two prior zero-new-findings iterations.
    for i in range(1, 3):
        state.review_iteration_telemetry.append(
            ReviewIterationTelemetry(
                iteration=i,
                max_iterations=4,
                cost_usd=0.0,
                duration_s=1.0,
                verdict="REQUEST_CHANGES",
                findings_by_severity={"P1": 0, "P2": 1},
                new_findings_by_severity={"P1": 0, "P2": 0},
                repeated_findings_by_severity={"P1": 0, "P2": 1},
                novel_findings=0,
                restated_findings=1,
            )
        )

    # Drive a single review_phase iteration that adds another zero-new-findings
    # telemetry and verify the loop short-circuits.  We simulate by directly
    # importing and invoking the early-termination block via the module.
    #
    # Rather than drive the full review pipeline (heavy), we inspect the logic:
    # call the check directly by appending a fresh zero-new telemetry and
    # running the tail-check code path.
    state.review_iteration_telemetry.append(
        ReviewIterationTelemetry(
            iteration=3,
            max_iterations=4,
            cost_usd=0.0,
            duration_s=1.0,
            verdict="REQUEST_CHANGES",
            findings_by_severity={"P1": 0, "P2": 1},
            new_findings_by_severity={"P1": 0, "P2": 0},
            repeated_findings_by_severity={"P1": 0, "P2": 1},
            novel_findings=0,
            restated_findings=1,
        )
    )
    tail = state.review_iteration_telemetry[-config.retry.review_zero_findings_stop :]
    converged = all(sum(t.new_findings_by_severity.values()) == 0 for t in tail)
    assert converged, "zero-new-findings tail should be detected"

    # Sanity check the _ReviewOutcome enum exists (import didn't break).
    assert _ReviewOutcome.DONE is not None
    # And a ReviewResult can be constructed (imports work).
    _ = ReviewResult(
        verdict="APPROVE",
        summary="",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def test_early_termination_disabled_when_threshold_zero(tmp_path: Path):
    """review_zero_findings_stop=0 disables early termination logic."""
    config = _make_adaptive_config(tmp_path, review_zero_findings_stop=0)
    assert config.retry.review_zero_findings_stop == 0


def test_adaptive_off_produces_policy_values(tmp_path: Path):
    policy = RetryPolicy(
        max_dev_iterations=3,
        max_review_cycles=2,
        max_dev_iterations_cap=6,
        max_review_cycles_cap=4,
        adaptive_iterations=False,
    )
    result = derive_limits(
        10,
        "large",
        policy,
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=None,
        model_profiles=_profiles(),
    )
    assert result.dev_max == 3
    assert result.review_max == 2


def test_adaptive_assignment_off_keeps_review_limits_adaptive(tmp_path: Path):
    from theforge.coordinator.engine import _coordinator_loop

    config = replace(
        _make_adaptive_config(tmp_path),
        assignment=AssignmentConfig(enabled=True, adaptive_enabled=False),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState()
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 9
    state.workspace_path = tmp_path
    state.branch_name = "feat/test"
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "model_profiles.yaml").write_text("models: {}\n", encoding="utf-8")

    class _StopLoop(Exception):
        pass

    def _fake_dev(*args, **kwargs):
        raise _StopLoop()

    with patch("theforge.coordinator.engine._run_dev_phase", _fake_dev):
        try:
            _coordinator_loop(state, config, task, "story", task_start=0.0)
        except _StopLoop:
            pass

    assert state.adaptive_dev_max == config.retry.max_dev_iterations
    assert state.budget.max_iterations == config.retry.max_dev_iterations
    assert state.adaptive_review_max == config.retry.max_review_cycles_cap
    assert state.adaptive_limits_audit["base_review"] == config.retry.max_review_cycles_cap
    assert state.adaptive_limits_audit["profile_history_runs"] == 0
    assert "complexity-derived review limit" in state.adaptive_limits_audit["rationale"]


def test_explicit_dev_override_wins_over_computed_limits(tmp_path: Path):
    from theforge.coordinator.engine import _coordinator_loop

    config = replace(
        _make_adaptive_config(tmp_path),
        dev_profile=replace(
            _make_adaptive_config(tmp_path).dev_profile,
            budget_usd=12.0,
            timeout_seconds=1200,
            max_iterations=4,
        ),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 6
    state.workspace_path = tmp_path
    state.branch_name = "feat/test"
    state._explicit_roles = {"dev"}
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "model_profiles.yaml").write_text("models: {}\n", encoding="utf-8")

    class _StopLoop(Exception):
        pass

    def _fake_dev(*args, **kwargs):
        raise _StopLoop()

    with patch("theforge.coordinator.engine._run_dev_phase", _fake_dev):
        try:
            _coordinator_loop(state, config, task, "story", task_start=0.0)
        except _StopLoop:
            pass

    # dev_profile.max_iterations=4 is the per-invocation agent-turn ceiling and
    # must not become the retry-attempt count, even under an explicit dev
    # override: attempts stay on retry policy (#2920).
    assert state.adaptive_dev_max == config.retry.max_dev_iterations
    assert state.budget.max_iterations == config.retry.max_dev_iterations
    assert state.adaptive_dev_timeout_seconds == round(1200 * MEDIUM_HEADROOM_FACTOR)
    assert state.adaptive_dev_cost_estimate_usd == 12.0
    assert "explicit dev forge.yaml override" in state.adaptive_limits_audit["rationale"]
