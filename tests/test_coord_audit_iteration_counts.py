"""Tests for audit iteration count accuracy (issue-601).

Verifies:
- dev_attempts_total counts all dev invocations
- dev_iterations_productive counts productive dev calls
- review_cycles_total is a separate distinct field
- Each dev telemetry entry carries a (cycle, iteration) tuple
- "iteration" means one thing per audit record: the monotonic gate trace
  counter is named ``trace_index`` and review_loop iteration matches the
  rendered reviews[].cycle (issue-1986)
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from coord_test_helpers import patch_gate_shell

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.review_phase import _record_review_iteration_telemetry
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    DevIterationTelemetry,
    Phase,
    ReviewCycleMetadata,
)
from theforge.review import ReviewResult
from theforge.runners import AgentResult


def _make_config(tmp_path: Path):
    from theforge.config import (
        DEFAULT_DEV_PROFILE,
        DEFAULT_PREFLIGHT_PROFILE,
        DEFAULT_REVIEW_PROFILE,
        ForgeConfig,
        RetryPolicy,
        ValidationConfig,
        WorkspaceConfig,
    )

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


def _make_task(tmp_path: Path):
    from theforge.task import TaskStory

    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Test spec", encoding="utf-8")
    return TaskStory(name="Test Task", slug="test-task", story_path=spec_path)


def _agent_result(cost: float = 0.10, profile: str = "dev") -> AgentResult:
    return AgentResult(
        success=True,
        output="done",
        session_id=None,
        cost_usd=cost,
        exit_code=0,
        raw={},
        profile_name=profile,
    )


def _make_result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


class TestIterationCountFields:
    """AC1: audit records dev_attempts_total, dev_iterations_productive, review_cycles_total."""

    def test_no_calls_all_zero(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["iterations"]["dev_attempts_total"] == 0
        assert log["iterations"]["dev_iterations_productive"] == 0
        assert log["iterations"]["review_cycles_total"] == 0
        assert log["totals"]["dev_attempts_total"] == 0
        assert log["totals"]["dev_iterations_productive"] == 0
        assert log["totals"]["review_cycles_total"] == 0

    def test_productive_dev_only_no_handoff_fix(self, tmp_path: Path) -> None:
        """When no handoff-fix retries occur, dev_attempts_total == dev_iterations_productive."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result())
        state.dev_results.append(_agent_result())
        state.dev_durations.extend([10.0, 12.0])
        state.dev_iteration = 2  # 2 productive iterations
        state.review_cycle = 1

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["iterations"]["dev_attempts_total"] == 2
        assert log["iterations"]["dev_iterations_productive"] == 2
        assert log["iterations"]["review_cycles_total"] == 1
        assert log["totals"]["dev_attempts_total"] == 2
        assert log["totals"]["dev_iterations_productive"] == 2
        assert log["totals"]["review_cycles_total"] == 1

    def test_productive_count_survives_cycle_reset(self, tmp_path: Path) -> None:
        """dev_iterations_productive counts across ALL review cycles, not just the last.

        Regression test: state.dev_iteration resets to 0 on REQUEST_CHANGES; using it
        as the productive count would report 0 (or N from the final cycle) instead of
        the true cumulative total.  len(state.dev_results) is the correct source.
        """
        state = CoordinatorState()
        # Simulate 2 productive dev calls in cycle 1 + 1 productive call in cycle 2
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_durations.extend([10.0, 12.0, 9.0])
        # dev_iteration is reset to 0 by review_phase on REQUEST_CHANGES, then
        # incremented once for cycle 2's single dev call — final value is 1, not 3.
        state.dev_iteration = 1
        state.review_cycle = 2

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        # Must reflect all 3 productive calls, not just the last cycle's counter
        assert log["iterations"]["dev_iterations_productive"] == 3
        assert log["totals"]["dev_iterations_productive"] == 3
        # Backward-compat alias must also be correct
        assert log["iterations"]["dev_iterations"] == 3
        assert log["totals"]["dev_iterations"] == 3

    def test_dev_attempts_total_equals_productive_count(self, tmp_path: Path) -> None:
        """dev_attempts_total equals dev_iterations_productive when no handoff-fix retries."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_durations.extend([10.0, 12.0])
        state.dev_iteration = 2
        state.review_cycle = 1

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["iterations"]["dev_attempts_total"] == 2
        assert log["iterations"]["dev_iterations_productive"] == 2
        assert log["totals"]["dev_attempts_total"] == 2
        assert log["totals"]["dev_iterations_productive"] == 2

    def test_total_dev_cost(self, tmp_path: Path) -> None:
        """total_dev_cost property sums all dev result costs."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_results.append(_agent_result(cost=0.25))

        assert state.total_dev_cost == pytest.approx(1.25)


class TestCycleIterationTuple:
    """AC2: each dev telemetry entry has (cycle, iteration) tuple."""

    def test_cycle_field_present_in_dev_loop(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=1,
                max_iterations=3,
                cost_usd=0.50,
                duration_s=10.0,
                cycle=0,
                gate_result="FAIL",
            )
        )
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=1,
                max_iterations=3,
                cost_usd=0.60,
                duration_s=12.0,
                cycle=1,
                gate_result="PASS",
            )
        )

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        dev_loop = log["iterations"]["dev_loop"]
        assert len(dev_loop) == 2
        assert dev_loop[0]["cycle"] == 0
        assert dev_loop[0]["iteration"] == 1
        assert dev_loop[1]["cycle"] == 1
        assert dev_loop[1]["iteration"] == 1

    def test_cycle_defaults_to_zero(self, tmp_path: Path) -> None:
        """DevIterationTelemetry.cycle defaults to 0 for backward compat."""
        t = DevIterationTelemetry(iteration=1, max_iterations=3, cost_usd=None, duration_s=5.0)
        assert t.cycle == 0


def _review_result(verdict: str = "APPROVE") -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="ok",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _cycle_metadata() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(
        pool_models=["reviewer"],
        successful=["reviewer"],
        failed=[],
        synthesized=False,
    )


class TestGateTraceIndexNaming:
    """issue-1986: the gate trace counter is named distinctly from the per-cycle iteration."""

    def test_gate_entries_name_their_own_trace_file(self, tmp_path: Path) -> None:
        """The counter in the trace filename appears verbatim on the entry that wrote it.

        Seam test across the VALIDATE writers and the audit serializer: the gate
        debug and diagnostic passes run for real (shell stubbed) at a monotonic
        trace counter of 3, while the dev loop is in review cycle 2 with its
        per-cycle iteration reset to 1. Before #1986 both counters serialized as
        "iteration", so a trace path quoted from an escalation resolved to a file
        whose number matched no dev_loop entry.
        """
        from theforge.coordinator.gate import _run_gate_debug_command
        from theforge.gate_diagnostics import run_gate_diagnostic_pass

        config = _make_config(tmp_path)
        config = dataclasses.replace(
            config,
            validation=dataclasses.replace(
                config.validation,
                gate_debug_command="make gate-debug",
                gate_debug_timeout=5,
                gate_output_tail_chars=200,
                gate_diagnostic_budget=5,
                gate_diagnostic_per_test_timeout=1,
            ),
        )
        task = _make_task(tmp_path)

        state = CoordinatorState()
        # Cycle 1 ran two dev iterations; cycle 2's counter has reset to 1 while
        # the trace counter kept climbing to 3.
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=1, max_iterations=3, cost_usd=0.1, duration_s=1.0, cycle=1
            )
        )
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=2, max_iterations=3, cost_usd=0.1, duration_s=1.0, cycle=1
            )
        )
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=1, max_iterations=3, cost_usd=0.1, duration_s=1.0, cycle=2
            )
        )
        state.dev_trace_count = 3
        state.validate_durations.append(5.0)

        def shell_side_effect(cmd, cwd, **kwargs):
            body = "debug output" if cmd == "make gate-debug" else "diagnostic output"
            return False, body, 1, False

        with patch_gate_shell(side_effect=shell_side_effect):
            debug = _run_gate_debug_command(config, tmp_path, iter_num=state.dev_trace_count)
            diagnostic = run_gate_diagnostic_pass(
                config, tmp_path, task=task, iter_num=state.dev_trace_count
            )
        assert debug is not None and diagnostic is not None
        state.gate_debug_telemetry.append(debug)
        state.gate_diagnostic_telemetry.append(diagnostic)

        log = generate_audit_log(config, task, _make_result(state))

        dev_loop = log["iterations"]["dev_loop"]
        # The last dev iteration is cycle 2's first — the reset the two counters
        # disagreed over.
        assert (dev_loop[-1]["cycle"], dev_loop[-1]["iteration"]) == (2, 1)

        for key, suffix in (
            ("gate_debug", "gate-debug"),
            ("gate_diagnostic", "gate-diagnostic"),
        ):
            entry = log["iterations"][key][0]
            # No ambiguous "iteration" on a gate entry — only the trace counter.
            assert "iteration" not in entry
            assert entry["trace_index"] == 3
            assert entry["trace_path"] == f".forge/traces/3-{suffix}.txt"
            written = tmp_path / entry["trace_path"]
            assert written.exists(), f"{key} entry names a trace that was not written"
            # The filename's counter is the one the entry publishes.
            assert written.name.split("-", 1)[0] == str(entry["trace_index"])


class TestReviewIterationMatchesRenderedCycle:
    """issue-1986: review_loop[].iteration equals reviews[].cycle for the same cycle."""

    def test_validate_opened_cycle_does_not_skip_review_iteration(self, tmp_path: Path) -> None:
        """A coordinator-opened cycle must not renumber the reviewer cycles.

        VALIDATE's RETRY_DEV_NEW_CYCLE path increments ``state.review_cycle``
        without appending review telemetry (engine.py). Sourcing the telemetry's
        iteration from that counter recorded two consecutive reviewer cycles as
        1 and 3 while ``build_reviews()`` rendered them as cycle 1 and 2.
        """
        state = CoordinatorState()

        # Reviewer cycle 1.
        state.review_cycle = 1
        state.review_cycle_metadata.append(_cycle_metadata())
        state.review_results.append(_review_result("REQUEST_CHANGES"))
        _record_review_iteration_telemetry(
            state,
            _review_result("REQUEST_CHANGES"),
            review_cost=0.20,
            review_elapsed=10.0,
            max_iterations=3,
        )

        # VALIDATE bought a cycle for its own blocking finding: the counter
        # advances, no reviewer ran, no telemetry entry.
        state.review_cycle += 1
        state.validate_opened_review_cycles += 1

        # Reviewer cycle 2 (state.review_cycle is now 3).
        state.review_cycle += 1
        state.review_cycle_metadata.append(_cycle_metadata())
        state.review_results.append(_review_result("APPROVE"))
        _record_review_iteration_telemetry(
            state,
            _review_result("APPROVE"),
            review_cost=0.30,
            review_elapsed=12.0,
            max_iterations=3,
        )

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        review_loop = log["iterations"]["review_loop"]
        reviews = log["reviews"]
        assert [entry["iteration"] for entry in review_loop] == [1, 2]
        assert [entry["cycle"] for entry in reviews] == [1, 2]
        # Same recorded event, same number, whichever list you read it from.
        for tele, rendered in zip(review_loop, reviews, strict=True):
            assert tele["iteration"] == rendered["cycle"]
            assert tele["verdict"] == rendered["verdict"]


class TestCostAgentRoles:
    """Dev agent entries are tagged correctly in cost.agents."""

    def test_dev_role_in_agents(self, tmp_path: Path) -> None:
        """cost.agents entries for dev calls use role='dev'."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_durations.append(10.0)

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        agents = log["cost"]["agents"]
        roles = [a["role"] for a in agents]
        assert "dev" in roles
        assert all(a["role"] != "dev/handoff-fix" for a in agents)
