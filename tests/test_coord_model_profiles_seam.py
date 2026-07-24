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

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,  # noqa: E402
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
    state.preflight_cache_snapshot = {
        "worktree_head": "OK",
        "evaluation_base_branch": "main",
        "evaluation_base_branch_head": "OK",
    }
    return state


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
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
            max_cost_per_story_usd=30.0,
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
            max_cost_per_story_usd=30.0,
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
            max_cost_per_story_usd=30.0,
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
    assert audit["assignments"]["dev"]["model"] == state._adaptive_decision.dev.model
    assert audit["assignments"]["dev"]["source"] == state._adaptive_decision.dev.registry_source
    assert "dev" in audit["rationale"]
    assert "target_usd" in audit["per_story_routing_cost_target"]


def test_preflight_audit_keeps_strong_planner_when_strong_dev_forces_budget_pressure(
    tmp_path, monkeypatch
):
    """Seam: audit and runtime planner stay aligned when adaptive routing picks strong."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    config = replace(
        _make_config(tmp_path),
        agents=[
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
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            max_cost_per_story_usd=15.0,
            min_reviewers=1,
            max_reviewers=1,
            prefer_cross_provider=False,
        ),
        dev_profile_is_default=True,
        plan_model_is_default=True,
        review_pool_is_default=True,
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 9

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _pf._apply_preflight_config(config, state)

    audit = state.complexity_routing_audit
    assert audit is not None
    assert audit["assignments"]["planner"]["model"] == "opus"
    assert audit["rationale"]["planner"].startswith("complexity score 9 → tier strong")
    assert audit["role_sources"]["planner"] == "adaptive"
    assert audit["per_story_routing_cost_target"]["within_target"] is False
    assert "planner" in audit["per_story_routing_cost_target"].get("protected_roles", [])


def test_preflight_seam_profile_backed_pre_promotion_propagates(tmp_path, monkeypatch):
    """Seam (#158): a low recency-weighted band success rate in model_profiles.yaml
    pre-promotes the dev tier through preflight → assign_models → routing_decision,
    and stickies the promotion on sprint state.

    Covers convention 8: the config threshold + on-disk profile flow across the
    preflight ↔ assign_models boundary and land in both the audit block and the
    per-sprint promotion cache.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    # API-provider agents (no cli) so ANTHROPIC_API_KEY alone satisfies auth — the
    # credential scrub removes CLI binaries, which would otherwise fail cli agents.
    agents = [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
    ]
    config = replace(
        _make_config(tmp_path),
        agents=agents,
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            max_cost_per_story_usd=100.0,
            min_reviewers=1,
            max_reviewers=1,
            prefer_cross_provider=False,
            dev_promotion_threshold=0.60,
            dev_promotion_min_runs=5,
        ),
    )

    # Seed a profile whose recency-weighted MEDIUM dev rate is well below 0.60 over
    # >= 5 admissible runs → the sonnet (mid) dev must pre-promote to opus (strong).
    profiles_path = tmp_path / ".forge" / "model_profiles.yaml"
    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    recent = [0, 1, 0, 0, 0, 0]
    profiles_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "sonnet": {
                        "dev": {
                            "runs": 6,
                            "success_rate": 0.17,
                            "_recent": recent,
                            "by_complexity": {
                                "medium": {
                                    "runs": 6,
                                    "success_rate": 0.17,
                                    "_recent": recent,
                                }
                            },
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    _pf._apply_preflight_config(config, state)

    # Dev pre-promoted mid → strong before the first iteration, driven entirely
    # by the recency-weighted profile signal (no separate sprint promotion cache).
    assert state._adaptive_decision.dev.model == "opus"
    promo = state.routing_decision["dev"]["promotion_check"]
    assert promo["fired"] is True
    assert promo["outcome"] == "promoted_below_threshold"
    assert promo["weighted_success_rate"] < promo["threshold"]
    assert promo["sample_size"] >= promo["min_runs"]  # fired only with admissible evidence
    assert promo["resulting_tier"] == "strong"


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
                max_cost_per_story_usd=100.0,
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


def test_explicit_planner_and_review_pool_threaded_into_assignment(tmp_path, monkeypatch):
    """Explicit planner + review_pool must show as overrides in audit and budget total."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from dataclasses import replace as _replace

    from theforge.config import ModelProfile, PlanConfig
    from theforge.coordinator import preflight as _pf

    explicit_planner_model = "explicit-planner-model"
    explicit_reviewer = ModelProfile(
        name="custom-reviewer",
        cli="claude",
        provider=None,
        model="custom-reviewer-model",
        budget_usd=2.5,
        timeout_seconds=300,
        allowed_tools=("Read", "Grep"),
    )

    config = _replace(
        _make_config(tmp_path),
        agents=[
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
                name="opus",
                provider="anthropic",
                model="opus",
                budget_usd=8.0,
                timeout_seconds=1200,
                tier="strong",
                cli="claude",
            ),
        ],
        plan=PlanConfig(
            enabled=True,
            cli="claude",
            model=explicit_planner_model,
            provider=None,
            budget_usd=7.5,
            timeout=600,
        ),
        plan_model_is_default=False,
        review_pool=[explicit_reviewer, explicit_reviewer],
        review_pool_is_default=False,
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            max_cost_per_story_usd=20.0,
            min_reviewers=1,
            max_reviewers=2,
            prefer_cross_provider=False,
        ),
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    _pf._apply_preflight_config(config, state)

    audit = state.complexity_routing_audit
    assert audit is not None
    # planner and code_review must be flagged as explicit overrides.
    assert audit["role_sources"]["planner"] == "explicit_override"
    assert audit["role_sources"]["code_review"] == "explicit_override"
    # The audit's recorded planner/code_reviewers must match what runtime uses.
    assert audit["assignments"]["planner"]["model"] == explicit_planner_model
    assert [entry["model"] for entry in audit["assignments"]["code_reviewers"]] == [
        explicit_reviewer.model,
        explicit_reviewer.model,
    ]
    # Budget total reflects the explicit pool, not the adaptive single-reviewer count.
    decision = state._adaptive_decision
    runtime_total = (
        decision.preflight.budget_usd
        + decision.planner.budget_usd
        + sum(p.budget_usd for p in decision.plan_reviewers)
        + decision.dev.budget_usd
        + sum(p.budget_usd for p in decision.code_reviewers)
    )
    assert audit["per_story_routing_cost_target"]["final_total_usd"] == round(runtime_total, 2)

    # #1391 iter1: the persisted routing_decision block must reflect the FULL
    # spliced explicit reviewer pool, not the single pre-splice override profile.
    block = state.routing_decision
    assert block is not None
    assert block["code_review"]["final"]["models"] == [
        explicit_reviewer.model,
        explicit_reviewer.model,
    ]
    # And the running reviewer appears as an included candidate — no
    # included-vs-final contradiction the reconstructability contract forbids.
    cr_pool = {e["name"]: e for e in block["code_review"]["candidate_pool"]}
    assert cr_pool[explicit_reviewer.name]["included"] is True
    assert cr_pool[explicit_reviewer.name]["reason"] == "none"
    # Every final model is reconstructable as an included candidate.
    included = {e["name"] for e in block["code_review"]["candidate_pool"] if e["included"]}
    assert explicit_reviewer.name in included


def test_explicit_review_pool_records_override_forced_overrun_when_over_cap(tmp_path, monkeypatch):
    """An expensive explicit review_pool must surface override_forced_overrun in audit."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from dataclasses import replace as _replace

    from theforge.config import ModelProfile
    from theforge.coordinator import preflight as _pf

    pricey_reviewer = ModelProfile(
        name="pricey-reviewer",
        cli="claude",
        provider=None,
        model="pricey-model",
        budget_usd=50.0,
        timeout_seconds=300,
        allowed_tools=("Read",),
    )

    config = _replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="haiku",
                provider="anthropic",
                model="haiku",
                budget_usd=1.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            ),
        ],
        review_pool=[pricey_reviewer, pricey_reviewer],
        review_pool_is_default=False,
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            max_cost_per_story_usd=5.0,
            min_reviewers=1,
            max_reviewers=2,
            prefer_cross_provider=False,
        ),
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    _pf._apply_preflight_config(config, state)

    audit = state.complexity_routing_audit
    assert audit is not None
    assert audit["role_sources"]["code_review"] == "explicit_override"
    assert audit["per_story_routing_cost_target"]["within_target"] is False
    assert audit["per_story_routing_cost_target"]["override_forced_overrun"] is True
    assert "code_review" in audit["per_story_routing_cost_target"]["locked_roles"]


def test_models_path_with_explicit_plan_and_review_pool_preserved(tmp_path, monkeypatch):
    """Explicit plan + review_pool overrides must win even when config.models is set.

    Regression guard for the pattern bug: the override-collection block was
    guarded by `if config.models is None`, so a models: config with additional
    explicit plan/review_pool overrides silently used adaptive selections instead.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from dataclasses import replace as _replace

    from theforge.config import ModelProfile, PlanConfig
    from theforge.coordinator import preflight as _pf

    explicit_plan_model = "pinned-planner-model"
    explicit_reviewer = ModelProfile(
        name="pinned-reviewer",
        cli="claude",
        provider=None,
        model="pinned-reviewer-model",
        budget_usd=2.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Grep"),
    )

    config = _replace(
        _make_config(tmp_path),
        # Simulate a v0.8 models: path by setting config.models to a non-None list.
        # Use an empty list so _apply_complexity_adaptation exits early while
        # still setting the "models path" branch of the code.
        models=[],
        agents=[
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
                name="opus",
                provider="anthropic",
                model="opus",
                budget_usd=8.0,
                timeout_seconds=1200,
                tier="strong",
                cli="claude",
            ),
        ],
        plan=PlanConfig(
            enabled=True,
            cli="claude",
            model=explicit_plan_model,
            provider=None,
            budget_usd=6.0,
            timeout=600,
        ),
        plan_model_is_default=False,
        review_pool=[explicit_reviewer],
        review_pool_is_default=False,
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            max_cost_per_story_usd=50.0,
            min_reviewers=1,
            max_reviewers=2,
            prefer_cross_provider=False,
        ),
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    _pf._apply_preflight_config(config, state)

    audit = state.complexity_routing_audit
    assert audit is not None
    # Planner must show the explicit model, not an adaptive selection.
    assert audit["assignments"]["planner"]["model"] == explicit_plan_model
    assert audit["role_sources"]["planner"] == "explicit_override"
    # Code reviewer must show the explicit pool, not an adaptive selection.
    assert [entry["model"] for entry in audit["assignments"]["code_reviewers"]] == [
        explicit_reviewer.model
    ]
    assert audit["role_sources"]["code_review"] == "explicit_override"
    # The adaptive decision must also have the explicit planner so plan_flow
    # will see it when "planner" is in _explicit_roles.
    decision = state._adaptive_decision
    assert decision.planner.model == explicit_plan_model
    assert [p.model for p in decision.code_reviewers] == [explicit_reviewer.model]


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


def test_record_run_memory_writes_profiles_and_no_legacy_history(tmp_path):
    """Helper writes model_profiles.yaml; legacy assignment_history.yaml is NOT written.

    Escalation memory now flows through the audit substrate (each run writes a
    canonical audit row; escalation history is derived from those rows). This
    test guards against regressing back to the old YAML-write path.
    """
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
            max_cost_per_story_usd=30.0,
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
    assert not history_path.exists(), (
        "assignment_history.yaml is now derived from the audit substrate, "
        "not written on each completed run (#793)."
    )


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


def test_apply_preflight_emits_terminal_warning_on_cap_downgrade(tmp_path, monkeypatch):
    """AC: explicit cap that downgrades selection prints a visible terminal warning.

    Verifies the seam between assign_models's downgrade detection and the
    preflight log printer. The warning must name the story, the cap, and
    both adaptive's selection and the post-cap result.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap", cli="claude"),
            AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid", cli="claude"),
            AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong", cli="claude"),
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            max_cost_per_story_usd=10.0,
            min_reviewers=1,
            max_reviewers=2,
            prefer_cross_provider=False,
        ),
        dev_profile_is_default=True,
        plan_model_is_default=True,
        review_pool_is_default=True,
    )

    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 9

    captured: list[str] = []
    _pf._apply_preflight_config(config, state, log=captured.append, task_slug="story-test")

    blob = "\n".join(captured)
    assert "per-story routing cost target" in blob
    assert "story-test" in blob
    assert "adaptive selected:" in blob
    assert "after target:" in blob
    # Wording rule: cap warning lines must never use the word "budget"
    for line in captured:
        if "per-story routing cost target" in line or "after target:" in line:
            assert "budget" not in line.lower(), f"Cap warning must not say 'budget': {line!r}"


def test_apply_preflight_skips_warning_when_cap_unset(tmp_path, monkeypatch):
    """AC: with cap unset, a high-complexity story emits no cap downgrade warning."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    from theforge.coordinator import preflight as _pf

    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap", cli="claude"),
            AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid", cli="claude"),
            AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong", cli="claude"),
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            max_cost_per_story_usd=None,  # unset — adaptive runs free
            min_reviewers=1,
            max_reviewers=2,
            prefer_cross_provider=False,
        ),
        dev_profile_is_default=True,
        plan_model_is_default=True,
        review_pool_is_default=True,
    )

    state = CoordinatorState()
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 9

    captured: list[str] = []
    _pf._apply_preflight_config(config, state, log=captured.append, task_slug="story-unset")

    blob = "\n".join(captured)
    assert "forces downgrade" not in blob
    assert "after target:" not in blob
    # Adaptive's selection survives — strong dev tier preserved
    assert state._adaptive_decision.dev.model == "opus"
    assert state._adaptive_decision.budget_audit["target_usd"] is None
    assert state._adaptive_decision.budget_audit["downgraded"] is False


def _dev_state_medium() -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5
    state.dev_trace_count = 3
    state.dev_durations = [40.0, 40.0, 40.0]
    return state


def _apply(config, state, success):
    """Seam: bridge.build_run_outcome → model_profiles.apply_run."""
    from theforge.coordinator.model_profiles_bridge import build_run_outcome
    from theforge.model_profiles import apply_run

    data: dict = {"models": {}}
    outcome = build_run_outcome(config, state, success=success)
    return apply_run(data, outcome), outcome


def test_seam_harness_kill_does_not_contaminate_capability_stats(tmp_path):
    """Seam (dev_phase flag → bridge → aggregator): a harness-imposed kill set on
    CoordinatorState must not move the dev bucket's success_rate/avg_iterations/
    _successes/runs versus a baseline with no kill — it is recorded separately in
    harness_terminated instead (#1763)."""
    from dataclasses import replace as _replace

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
    )

    def _dev_section(data):
        entry = next(m for m in data["models"].values() if "dev" in m)
        return entry["dev"]

    # Baseline: a single clean completed run.
    baseline_state = _dev_state_medium()
    baseline_data, _ = _apply(config, baseline_state, success=True)
    base_dev = _dev_section(baseline_data)

    # Timeout kill: dev_phase set the sticky flag before falling through.
    killed_state = _dev_state_medium()
    killed_state.adaptive_dev_timeout_seconds = 900
    killed_state.dev_process_timeout_killed = True
    killed_data, killed_outcome = _apply(config, killed_state, success=False)
    killed_dev = _dev_section(killed_data)

    # The bridge carried the taxonomy across the seam.
    assert killed_outcome.dev_termination_cause == "timeout"

    # Capability stats: the kill starts from an empty profile, so a contaminated
    # fold would show runs=1/success_rate=0.0. Segregation keeps them at zero.
    assert killed_dev.get("runs", 0) == 0
    assert killed_dev.get("_successes", 0) == 0
    assert killed_dev.get("success_rate", 0.0) == 0.0
    assert killed_dev.get("avg_iterations", 0.0) == 0.0
    # ...while the clean baseline recorded a real success.
    assert base_dev["runs"] == 1 and base_dev["success_rate"] == 1.0

    # The kill is recorded, and the timeout floor still moves.
    bc = killed_dev["by_complexity"]["medium"]
    assert bc["harness_terminated"]["runs"] == 1
    assert bc["harness_terminated"]["by_cause"] == {"timeout": 1}
    assert bc["max_killed_timeout_s"] == 900.0

    # Stuck-terminate flows through the same seam under its own cause, with no floor.
    stuck_state = _dev_state_medium()
    stuck_state.dev_process_stuck_terminated = True
    stuck_data, stuck_outcome = _apply(config, stuck_state, success=False)
    assert stuck_outcome.dev_termination_cause == "stuck_pattern"
    stuck_bc = _dev_section(stuck_data)["by_complexity"]["medium"]
    assert stuck_bc["harness_terminated"]["by_cause"] == {"stuck_pattern": 1}
    assert stuck_bc.get("runs", 0) == 0
    assert stuck_bc.get("max_killed_timeout_s", 0.0) == 0.0


def _preflight_result_with_attempts(attempts: list[dict]):
    """A preflight AgentResult carrying a native raw['attempts'] list."""
    return replace(_make_agent_result(success=True), raw={"attempts": attempts})


def _planner_profile():
    from theforge.config import ModelProfile

    return ModelProfile(
        name="pl",
        cli="claude",
        provider=None,
        model="opus",
        budget_usd=8.0,
        timeout_seconds=1200,
        allowed_tools=("Read",),
    )


def test_seam_bridge_derives_preflight_and_planner_reliability(tmp_path):
    """Seam (state → bridge): build_run_outcome derives one RoleAttempt per native
    preflight/planner invocation (#1489). A cached preflight and a story that never
    planned record nothing."""
    from types import SimpleNamespace

    from theforge.coordinator.model_profiles_bridge import build_run_outcome

    config = _make_config(tmp_path)
    state = _dev_state_medium()
    state.preflight_result = _preflight_result_with_attempts(
        [
            {
                "profile_name": "preflight",
                "model": "sonnet",
                "provider": "anthropic",
                "cli": None,
                "cost_usd": 0.01,
                "success": True,
                "completed": True,
            }
        ]
    )
    planner = _planner_profile()
    state._adaptive_decision = SimpleNamespace(planner=planner)
    state.plan_results = [_make_agent_result(success=True)]

    outcome = build_run_outcome(config, state, success=True)
    assert len(outcome.preflight_attempts) == 1
    assert outcome.preflight_attempts[0].completed is True
    assert outcome.preflight_attempts[0].name == "preflight"
    assert outcome.planner_model == "pl"
    assert [a.completed for a in outcome.planner_attempts] == [True]

    # A cached preflight ran no model this story → no attempt recorded.
    state.preflight_cached = True
    assert build_run_outcome(config, state, success=True).preflight_attempts == []

    # No planning attempted → no planner telemetry (identity/cost suppressed too).
    bare = _dev_state_medium()
    bare._adaptive_decision = SimpleNamespace(planner=planner)
    outcome_bare = build_run_outcome(config, bare, success=True)
    assert outcome_bare.planner_model is None
    assert outcome_bare.planner_attempts == []


def test_seam_preflight_parse_retry_and_fallback_attributed_per_model(tmp_path):
    """Seam (preflight_flow attempts → bridge → aggregator): a failed primary +
    successful fallback folds a failure for the primary and a success for the
    fallback — the #1489 P1 fix, proven end to end."""
    from types import SimpleNamespace

    from theforge.coordinator.model_profiles_bridge import build_run_outcome
    from theforge.model_profiles import apply_run

    config = _make_config(tmp_path)
    state = _dev_state_medium()
    state.preflight_result = _preflight_result_with_attempts(
        [
            # primary parse-degraded, then crashed on retry, then fallback succeeds.
            {
                "profile_name": "preflight",
                "model": "sonnet",
                "provider": "anthropic",
                "cli": None,
                "cost_usd": 0.01,
                "success": True,
                "completed": False,
            },
            {
                "profile_name": "preflight",
                "model": "sonnet",
                "provider": "anthropic",
                "cli": None,
                "cost_usd": 0.01,
                "success": False,
                "completed": False,
            },
            {
                "profile_name": "preflight-fallback",
                "model": "haiku",
                "provider": "anthropic",
                "cli": None,
                "cost_usd": 0.005,
                "success": True,
                "completed": True,
            },
        ]
    )
    state._adaptive_decision = SimpleNamespace(planner=_planner_profile())

    outcome = build_run_outcome(config, state, success=True)
    assert [a.completed for a in outcome.preflight_attempts] == [False, False, True]

    data: dict = {"models": {}}
    apply_run(data, outcome)
    primary = data["models"]["anthropic/sonnet/api"]["preflight"]
    assert primary["_attempted_count"] == 2
    assert primary["_completed_count"] == 0  # primary never improved by the fallback
    fallback = data["models"]["anthropic/haiku/api"]["preflight"]
    assert fallback["_attempted_count"] == 1
    assert fallback["_completed_count"] == 1


def test_seam_planner_transport_retry_folds_failed_attempt(tmp_path):
    """Seam: a planner transport-retry failure contributes a failed reliability
    attempt rather than being hidden by the later successful plan output (#1489)."""
    from types import SimpleNamespace

    from theforge.coordinator.model_profiles_bridge import build_run_outcome
    from theforge.model_profiles import apply_run

    config = _make_config(tmp_path)
    state = _dev_state_medium()
    state._adaptive_decision = SimpleNamespace(planner=_planner_profile())
    # One transport retry (failure) then a successful plan generation.
    state.plan_transport_retries = [{"phase": "PLAN", "attempt": 0, "retry": 1, "error": "5xx"}]
    state.plan_results = [_make_agent_result(success=True)]
    state.plan_output = "the plan"

    outcome = build_run_outcome(config, state, success=True)
    assert [a.completed for a in outcome.planner_attempts] == [False, True]

    data: dict = {"models": {}}
    apply_run(data, outcome)
    # planner profile uses cli="claude" → canonical transport is "cli".
    pl = data["models"]["anthropic/opus/cli"]["planner"]
    assert pl["_attempted_count"] == 2
    assert pl["_completed_count"] == 1
