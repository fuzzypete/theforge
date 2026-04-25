"""Seam tests for the engine ↔ model_profiles + preflight ↔ assign_models paths.

These cover the coordinator boundaries where model profile data flows:
  1. engine.run_task writes model_profiles.yaml after a run regardless of the
     escalation_memory feature flag (AC: "updated after each run").
  2. preflight._apply_preflight_config loads model_profiles.yaml and hands it
     to assign_models (AC: "assignment system reads profiles when making
     decisions").
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import (  # noqa: E402
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (  # noqa: E402
    AgentDef,
    AssignmentConfig,
)
from theforge.coordinator.engine import run_task  # noqa: E402
from theforge.coordinator.state import CoordinatorState  # noqa: E402


def _cached_proceed_state_with_complexity(complexity: str = "medium") -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    state.preflight_reason = "ok"
    state.preflight_complexity = complexity
    state.preflight_complexity_score = 5
    state.preflight_sufficiency = "implementation_ready"
    state.preflight_work_type = "feature"
    return state


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch("theforge.coordinator.util._run_shell")
def test_model_profiles_written_even_when_escalation_memory_disabled(
    mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
):
    """AC: model_profiles.yaml is updated after every run, independent of
    the escalation_memory flag."""
    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="claude-sonnet",
                provider=None,
                model="sonnet",
                budget_usd=10.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            )
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,  # deliberately off — profiles must still write
            budget_per_story_usd=30.0,
        ),
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()
    cached_state = _cached_proceed_state_with_complexity()

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_plan_agent.side_effect = mock_agent
    mock_preflight.return_value = _PREFLIGHT_RESULT
    mock_agent.return_value = _make_agent_result(success=True, output="implemented")
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task, cached_preflight_state=cached_state)

    assert result.success is True
    profiles_path = tmp_path / ".forge" / "model_profiles.yaml"
    assert profiles_path.exists(), "model_profiles.yaml should be written each run"

    # And escalation_memory is OFF, so assignment_history.yaml should NOT exist.
    history_path = tmp_path / ".forge" / "assignment_history.yaml"
    assert not history_path.exists()

    data = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
    models = data.get("models") or {}
    # The dev profile name from _make_config — whatever it is, it's recorded.
    assert models, "at least one model should be recorded"
    dev_entries = [m for m in models.values() if "dev" in m]
    assert dev_entries, "the dev model's dev role should be aggregated"


def test_preflight_passes_model_profiles_to_assign_models(tmp_path, monkeypatch):
    """AC: the assignment system reads model_profiles when deciding.

    Verifies preflight._apply_preflight_config loads model_profiles.yaml and
    forwards it as the model_profiles kwarg to assign_models.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="claude-sonnet",
                provider="anthropic",
                model="sonnet",
                budget_usd=10.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            )
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            budget_per_story_usd=30.0,
            min_reviewers=1,
            max_reviewers=1,
        ),
    )

    # Seed a pre-existing model_profiles.yaml so we can assert it is what's passed.
    profiles_path = tmp_path / ".forge" / "model_profiles.yaml"
    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    seeded = {
        "models": {
            "claude-sonnet": {
                "dev": {
                    "runs": 5,
                    "success_rate": 0.8,
                    "by_complexity": {"medium": {"runs": 5, "success_rate": 0.8}},
                }
            }
        }
    }
    profiles_path.write_text(yaml.safe_dump(seeded), encoding="utf-8")

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    captured: dict = {}

    def _fake_assign_models(*args, **kwargs):
        captured["model_profiles"] = kwargs.get("model_profiles")
        captured["complexity_score"] = kwargs.get("complexity_score")
        # Return a minimal decision with the configured dev profile to avoid
        # exercising the real logic.
        from theforge.assignment import AssignmentDecision

        return AssignmentDecision(
            preflight=config.preflight_profile,
            planner=config.dev_profile,
            plan_reviewers=[],
            dev=config.dev_profile,
            code_reviewers=[],
            rationale={},
        )

    monkeypatch.setattr("theforge.assignment.assign_models", _fake_assign_models)

    _pf._apply_preflight_config(config, state)

    assert "model_profiles" in captured
    assert captured["model_profiles"] == seeded
    assert captured["complexity_score"] == 5


def test_preflight_records_assignment_rationale_in_audit_state(tmp_path, monkeypatch):
    """Adaptive assignment details must be visible on state for audit rendering."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="claude-sonnet",
                provider="anthropic",
                model="sonnet",
                budget_usd=10.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            )
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            budget_per_story_usd=30.0,
            min_reviewers=1,
            max_reviewers=1,
        ),
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 7

    _pf._apply_preflight_config(config, state)

    audit = state.complexity_routing_audit
    assert audit is not None
    assert audit["complexity_score"] == 7
    assert audit["source"] == "adaptive_assignment"
    assert audit["adaptive_enabled"] is True
    assert audit["role_sources"]["dev"] == "adaptive"
    assert audit["assignments"]["dev"] == state._adaptive_decision.dev.model
    assert "dev" in audit["rationale"]
    assert "budget_cap_usd" in audit["budget"]


def test_preflight_seam_adaptive_on_vs_off_diverges_then_converges(tmp_path, monkeypatch):
    """Seam: adaptive_enabled toggles between score-aware routing and static bands."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    base_agents = [
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
            cli="claude",
        ),
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
            cli="claude",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
            cli="claude",
        ),
    ]

    def _build(adaptive_enabled: bool):
        return replace(
            _make_config(tmp_path),
            agents=base_agents,
            assignment=AssignmentConfig(
                enabled=True,
                escalation_memory=False,
                budget_per_story_usd=100.0,
                min_reviewers=1,
                max_reviewers=3,
                prefer_cross_provider=False,
                adaptive_enabled=adaptive_enabled,
            ),
        )

    def _state_with_score(score: int) -> CoordinatorState:
        s = CoordinatorState()
        s.preflight_complexity = "medium"
        s.preflight_complexity_score = score
        return s

    # Adaptive ON: scores 2 and 9 should diverge.
    cfg_on = _build(True)
    state_low = _state_with_score(2)
    state_high = _state_with_score(9)
    _pf._apply_preflight_config(cfg_on, state_low)
    _pf._apply_preflight_config(cfg_on, state_high)
    assert state_low._adaptive_decision.dev.model != state_high._adaptive_decision.dev.model
    assert state_low.complexity_routing_audit["adaptive_enabled"] is True
    assert state_low.complexity_routing_audit["role_sources"]["dev"] == "adaptive"

    # Adaptive OFF: same band → same assignment regardless of score.
    cfg_off = _build(False)
    state_low_off = _state_with_score(2)
    state_high_off = _state_with_score(9)
    _pf._apply_preflight_config(cfg_off, state_low_off)
    _pf._apply_preflight_config(cfg_off, state_high_off)
    assert (
        state_low_off._adaptive_decision.dev.model == state_high_off._adaptive_decision.dev.model
    )
    assert state_low_off.complexity_routing_audit["adaptive_enabled"] is False
    assert state_low_off.complexity_routing_audit["source"] == "static_assignment"
    assert state_low_off.complexity_routing_audit["role_sources"]["dev"] == "static"


def test_record_run_memory_is_called_from_resume_path(tmp_path):
    """Both run_task and _run_resume_coordinator must call _record_run_memory.

    Regression guard for cycle 2: resume entry points (run_from_review /
    run_from_dev) previously skipped model_profiles.yaml updates because the
    persistence logic lived inline in run_task. It now lives in a shared
    helper — this test asserts the helper is referenced from both paths.
    """
    from theforge.coordinator import engine as _eng

    source = Path(_eng.__file__).read_text(encoding="utf-8")
    # Exactly one definition + at least two call sites (run_task + resume).
    assert source.count("def _record_run_memory(") == 1
    assert source.count("_record_run_memory(") >= 3  # 1 def + 2 calls


def test_record_run_memory_writes_profiles_and_history(tmp_path):
    """Unit test of the shared helper: both files written when gates are met."""
    from dataclasses import replace as _replace

    from theforge.coordinator.engine import _record_run_memory
    from theforge.coordinator.state import CoordinatorResult, Phase

    config = _replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="claude-sonnet",
                provider=None,
                model="sonnet",
                budget_usd=10.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            )
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=True,
            budget_per_story_usd=30.0,
        ),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")

    _record_run_memory(config, task, state, result)

    profiles_path = tmp_path / ".forge" / "model_profiles.yaml"
    history_path = tmp_path / ".forge" / "assignment_history.yaml"
    assert profiles_path.exists()
    assert history_path.exists()


def test_record_run_memory_skips_when_preflight_complexity_missing(tmp_path, caplog):
    """Helper must no-op (and log) when preflight_complexity is unset."""
    import logging

    from theforge.coordinator.engine import _record_run_memory
    from theforge.coordinator.state import CoordinatorResult, Phase

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState()  # preflight_complexity stays None
    result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")

    with caplog.at_level(logging.DEBUG):
        _record_run_memory(config, task, state, result)

    profiles_path = tmp_path / ".forge" / "model_profiles.yaml"
    assert not profiles_path.exists()
