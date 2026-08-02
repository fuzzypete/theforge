"""Coordinator-level runtime integration tests for adaptive assignment (#269).

The pure selector (``assignment.assign_models``) and the preflight seam already
have coverage. What these tests add is *runtime* proof: a full ``run_task``
coordinator run, with only the agent-invocation boundary mocked, asserting that

  * the adaptive planner / review pool / plan reviewers are the profiles the
    coordinator actually *invokes*, not merely what the decision object holds;
  * an explicit ``forge.yaml`` override survives to the invocation AND is
    recorded as ``explicit_override_locked`` in the canonical ``routing_decision``
    block (#1391, ADR-0006 clauses 1 and 7);
  * escalation-history effects after DONE and ESCALATE are observable through the
    authoritative substrate/native run records and the ``routing_decision`` block
    — the legacy ``.forge/assignment_history.yaml`` snapshot is never written;
  * the persisted block for a real runtime path carries candidate pool, exclusion
    reasons, consulted signals, adaptive-mechanism outcomes, and final selection.

Every explanation assertion reads from the canonical block; no parallel audit
trail is introduced.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace as _dc_replace
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import (  # noqa: E402
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED_MEDIUM,
    REQUEST_CHANGES_REVIEW,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.assignment import (  # noqa: E402
    EXCLUSION_REASONS,
    REASON_EXPLICIT_OVERRIDE_LOCKED,
    REASON_NONE,
)
from theforge.config import (  # noqa: E402
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    AgentDef,
    AssignmentConfig,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import audit_substrate as _sub  # noqa: E402
from theforge.coordinator.engine import run_task  # noqa: E402
from theforge.coordinator.escalation_history import (  # noqa: E402
    load_escalation_history_from_substrate,
)

PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _api_keys(monkeypatch):
    """Agents are API-provider (no cli), so one key satisfies _has_auth()."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _agents() -> list[AgentDef]:
    """Pool whose model names cannot collide with any default profile model."""
    return [
        AgentDef(
            name="pool-cheap",
            provider="anthropic",
            model="pool-cheap-model",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
        ),
        AgentDef(
            name="pool-mid",
            provider="anthropic",
            model="pool-mid-model",
            budget_usd=4.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="pool-strong",
            provider="anthropic",
            model="pool-strong-model",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
    ]


DEFAULT_PLAN_MODEL = "unused-default-plan-model"
DEFAULT_REVIEW_MODEL = "unused-default-review-model"

_DEFAULT_REVIEWER = ModelProfile(
    name="default-reviewer",
    cli="claude",
    model=DEFAULT_REVIEW_MODEL,
    budget_usd=1.0,
    timeout_seconds=300,
    allowed_tools=("Read", "Grep"),
)


def _runtime_config(tmp_path: Path, **overrides) -> ForgeConfig:
    """Coordinator config with an adaptive agents pool and PLAN enabled.

    ``plan_model_is_default`` / ``review_pool_is_default`` default to True here:
    the "operator configured nothing" baseline the adaptive path is meant to own.
    Tests that exercise the override hierarchy flip them to False explicitly.
    """
    base = ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[_DEFAULT_REVIEWER],
        review_pool_is_default=True,
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig(
            enabled=True,
            cli="claude",
            model=DEFAULT_PLAN_MODEL,
            budget_usd=0.50,
            timeout=300,
            validate_spec=False,
        ),
        plan_model_is_default=True,
        agents=_agents(),
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=True,
            max_cost_per_story_usd=100.0,
            min_reviewers=1,
            max_reviewers=2,
            prefer_cross_provider=False,
        ),
        log=LogConfig(enabled=False),
    )
    return _dc_replace(base, **overrides) if overrides else base


def _stable_dev_config(tmp_path: Path, **overrides) -> ForgeConfig:
    """Runtime config with the post-plan dev-tier checkpoint disabled.

    The checkpoint (#1387) can demote the dev tier *after* plan review, so the
    model that runs differs from the preflight-time pick. Tests that follow one
    candidate's evidence across runs turn it off so the dev identity is stable
    between the routing_decision block and the invocation.
    """
    base = _runtime_config(tmp_path, **overrides)
    return _dc_replace(
        base,
        assignment=_dc_replace(base.assignment, plan_tier_reduction=False),
    )


class _Captured(dict):
    """Profiles the coordinator actually invoked, per role."""

    @property
    def plan_model(self) -> str:
        return self["plan"][0]

    @property
    def plan_review_models(self) -> list[str]:
        return self["plan_review"][0]

    @property
    def code_review_models(self) -> list[str]:
        return self["code_review"][0]


def _run_story(
    config: ForgeConfig,
    tmp_path: Path,
    *,
    review_outputs: list[str] | None = None,
    slug_dir: str = "test-task",
):
    """Drive a full ``run_task`` with only the agent boundary mocked.

    Returns ``(result, captured)`` where ``captured`` records the profiles the
    coordinator handed to each runner — the runtime truth the ACs are about.
    """
    task = _make_task(tmp_path)
    workspace = tmp_path / slug_dir
    workspace.mkdir(exist_ok=True)
    captured = _Captured(plan=[], plan_review=[], code_review=[], dev=[])
    review_outputs = review_outputs or [APPROVE_REVIEW]
    review_idx = {"n": 0}

    def _plan_agent(**kwargs):
        captured["plan"].append(kwargs["profile"].model)
        return _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.10)

    def _plan_review_pool(**kwargs):
        captured["plan_review"].append([p.model for p in kwargs["profiles"]])
        return [
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_APPROVE,
                cost_usd=0.05,
                profile_name=p.name,
            )
            for p in kwargs["profiles"]
        ]

    def _dev_agent(**kwargs):
        captured["dev"].append(kwargs["profile"].model)
        return _make_agent_result(success=True, output="Implemented.")

    def _code_review_pool(**kwargs):
        captured["code_review"].append([p.model for p in kwargs["profiles"]])
        out = review_outputs[min(review_idx["n"], len(review_outputs) - 1)]
        review_idx["n"] += 1
        return [
            _make_agent_result(success=True, output=out, profile_name=p.name)
            for p in kwargs["profiles"]
        ]

    with (
        patch_gate_shell(side_effect=_shell_with_gate(workspace, "PASS")),
        patch(
            "theforge.coordinator.preflight_flow.run_agent",
            return_value=_make_agent_result(
                success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
            ),
        ),
        patch("theforge.coordinator.plan_flow.run_agent", side_effect=_plan_agent),
        patch("theforge.coordinator.plan_flow.run_agent_pool", side_effect=_plan_review_pool),
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=_dev_agent),
        patch("theforge.coordinator.review_pool.run_agent_pool", side_effect=_code_review_pool),
    ):
        result = run_task(config, task)
    return result, captured


def _persist_run(result, config: ForgeConfig, tmp_path: Path) -> dict:
    """Persist the run through the production audit path and return the record.

    ``theforge.cli.shared._write_audit`` is the single production writer of the
    native per-run JSON record and its SQLite substrate mirror; the coordinator
    engine itself does not write one. Using it keeps this test on the
    authoritative surface rather than hand-building substrate rows.
    """
    from theforge.cli.shared import _write_audit

    task = _make_task(tmp_path)
    _write_audit(result, config, task)
    run_file = tmp_path / ".forge" / "audits" / "runs" / f"{result.state.run_id}.json"
    assert run_file.exists(), "native per-run record must be written"
    return json.loads(run_file.read_text(encoding="utf-8"))


def _pool_by_name(role_block: dict) -> dict[str, dict]:
    return {e["name"]: e for e in role_block["candidate_pool"]}


# ── AC1: adaptive planner when plan config is default ─────────────────


def test_run_task_uses_adaptive_planner_when_plan_config_is_default(tmp_path):
    """The PLAN phase invokes the adaptive planner, not the default plan model."""
    config = _runtime_config(tmp_path)

    result, captured = _run_story(config, tmp_path)

    assert result.success is True
    adaptive_planner = result.state._adaptive_decision.planner.model
    # Runtime proof: the model the plan agent RAN with is the adaptive pick.
    assert captured.plan_model == adaptive_planner
    assert captured.plan_model != DEFAULT_PLAN_MODEL
    assert captured.plan_model in {a.model for a in _agents()}

    # And the canonical block agrees with what ran (#1391).
    block = result.state.routing_decision
    assert block["planner"]["final"]["model"] == captured.plan_model
    assert result.state.complexity_routing_audit["role_sources"]["planner"] == "adaptive"
    # No candidate was locked out by an override that does not exist.
    reasons = {e["reason"] for e in block["planner"]["candidate_pool"]}
    assert REASON_EXPLICIT_OVERRIDE_LOCKED not in reasons


# ── AC2: explicit plan config preserved + explicit_override_locked ────


def test_run_task_preserves_explicit_plan_config_and_locks_the_block(tmp_path):
    """An explicit plan model runs unchanged and locks the planner candidate pool."""
    explicit_model = "operator-pinned-plan-model"
    config = _runtime_config(
        tmp_path,
        plan=PlanConfig(
            enabled=True,
            cli="claude",
            model=explicit_model,
            budget_usd=3.0,
            timeout=300,
            validate_spec=False,
        ),
        plan_model_is_default=False,
    )

    result, captured = _run_story(config, tmp_path)

    assert result.success is True
    # Preserved at runtime: the plan agent ran with the operator's model.
    assert captured.plan_model == explicit_model

    block = result.state.routing_decision
    assert block["planner"]["final"]["model"] == explicit_model
    # Recorded as explicit_override_locked in the canonical block: every adaptive
    # candidate is excluded, and the reason is the override lock — not a tier
    # mismatch or an auth gap.
    pool = _pool_by_name(block["planner"])
    assert set(pool) == {a.name for a in _agents()}
    for name, entry in pool.items():
        assert entry["included"] is False, name
        assert entry["reason"] == REASON_EXPLICIT_OVERRIDE_LOCKED, name
    assert result.state.complexity_routing_audit["role_sources"]["planner"] == "explicit_override"


# ── AC3: adaptive review pool replaces the default pool only when not
#        explicitly configured ───────────────────────────────────────


def test_run_task_adaptive_review_pool_replaces_default_pool(tmp_path):
    """With a default review pool, the reviewers that RUN are the adaptive ones."""
    config = _runtime_config(tmp_path)

    result, captured = _run_story(config, tmp_path)

    assert result.success is True
    adaptive_reviewers = [p.model for p in result.state._adaptive_decision.code_reviewers]
    assert adaptive_reviewers, "adaptive assignment must select reviewers"
    assert captured.code_review_models == adaptive_reviewers
    assert DEFAULT_REVIEW_MODEL not in captured.code_review_models

    block = result.state.routing_decision
    assert block["code_review"]["final"]["models"] == captured.code_review_models
    assert result.state.complexity_routing_audit["role_sources"]["code_review"] == "adaptive"


# ── AC4: explicit review pool preserved + explicit_override_locked ────


def test_run_task_preserves_explicit_review_pool_and_locks_the_block(tmp_path):
    """An explicit review_pool runs unchanged; adaptive candidates are locked out."""
    explicit_reviewers = [
        ModelProfile(
            name="pinned-reviewer-a",
            cli="claude",
            model="pinned-reviewer-a-model",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Grep"),
        ),
        ModelProfile(
            name="pinned-reviewer-b",
            cli="claude",
            model="pinned-reviewer-b-model",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Grep"),
        ),
    ]
    config = _runtime_config(
        tmp_path,
        review_pool=explicit_reviewers,
        review_pool_is_default=False,
    )

    result, captured = _run_story(config, tmp_path)

    assert result.success is True
    # Preserved at runtime: both operator reviewers ran, adaptive replaced nothing.
    assert captured.code_review_models == [p.model for p in explicit_reviewers]

    block = result.state.routing_decision
    assert block["code_review"]["final"]["models"] == [p.model for p in explicit_reviewers]
    pool = _pool_by_name(block["code_review"])
    # Every adaptive-registry candidate is locked out by the explicit override.
    for agent in _agents():
        assert pool[agent.name]["included"] is False, agent.name
        assert pool[agent.name]["reason"] == REASON_EXPLICIT_OVERRIDE_LOCKED, agent.name
    # Every reviewer that ran is reconstructable as an included candidate.
    for profile in explicit_reviewers:
        assert pool[profile.name]["included"] is True
        assert pool[profile.name]["reason"] == REASON_NONE
    assert (
        result.state.complexity_routing_audit["role_sources"]["code_review"] == "explicit_override"
    )


# ── AC5: adaptive plan reviewers replace plan_agent_review only when
#        not explicitly configured ────────────────────────────────────


def test_run_task_adaptive_plan_reviewers_replace_plan_agent_review(tmp_path):
    """plan_agent_review unset → the adaptive plan reviewers are the ones that run."""
    config = _runtime_config(tmp_path)
    assert config.plan_agent_review.enabled is False

    result, captured = _run_story(config, tmp_path)

    assert result.success is True
    adaptive_plan_reviewers = [p.model for p in result.state._adaptive_decision.plan_reviewers]
    assert adaptive_plan_reviewers, "adaptive assignment must select plan reviewers"
    assert captured.plan_review_models == adaptive_plan_reviewers
    assert result.state.plan_review_mode == "agent"
    block = result.state.routing_decision
    assert block["plan_review"]["final"]["models"] == adaptive_plan_reviewers


def test_run_task_preserves_explicit_plan_agent_review_pool(tmp_path):
    """An explicit plan_agent_review pool is preserved and locks its block."""
    pinned = ModelProfile(
        name="pinned-plan-reviewer",
        cli="claude",
        model="pinned-plan-reviewer-model",
        budget_usd=2.0,
        timeout_seconds=300,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
    )
    config = _runtime_config(
        tmp_path,
        plan_agent_review=PlanAgentReviewConfig(enabled=True, pool=[pinned]),
    )

    result, captured = _run_story(config, tmp_path)

    assert result.success is True
    assert captured.plan_review_models == [pinned.model]

    block = result.state.routing_decision
    assert block["plan_review"]["final"]["models"] == [pinned.model]
    pool = _pool_by_name(block["plan_review"])
    for agent in _agents():
        assert pool[agent.name]["included"] is False, agent.name
        assert pool[agent.name]["reason"] == REASON_EXPLICIT_OVERRIDE_LOCKED, agent.name
    assert pool[pinned.name]["included"] is True
    assert (
        result.state.complexity_routing_audit["role_sources"]["plan_review"] == "explicit_override"
    )


# ── AC6: escalation-history effects after DONE and ESCALATE ───────────


def test_escalation_history_after_done_and_escalate_flows_through_substrate(tmp_path):
    """A DONE run and an ESCALATE run must be observable to the router only
    through the authoritative substrate/native records and the routing_decision
    block — never through a standalone assignment-history YAML surface."""
    config = _stable_dev_config(tmp_path)

    done_result, done_captured = _run_story(config, tmp_path)
    assert done_result.success is True
    done_record = _persist_run(done_result, config, tmp_path)

    esc_result, esc_captured = _run_story(
        config,
        tmp_path,
        review_outputs=[REQUEST_CHANGES_REVIEW],
    )
    assert esc_result.success is False
    assert esc_result.phase.name == "ESCALATE"
    esc_record = _persist_run(esc_result, config, tmp_path)

    # 1. Native per-run records carry the authoritative outcomes.
    assert done_record["outcome"]["success"] is True
    assert esc_record["outcome"]["success"] is False
    assert esc_record["outcome"]["final_phase"] == "ESCALATE"

    # 2. The router's escalation history is derived from the substrate, and it
    #    sees both outcomes in chronological order.
    history = load_escalation_history_from_substrate(tmp_path)
    assert [r.outcome for r in history] == ["DONE", "ESCALATE"]
    assert {r.complexity for r in history} == {"MEDIUM"}
    # Both runs routed to the same dev model, so the two outcomes accumulate as
    # one candidate's evidence.
    dev_model_ran = done_captured["dev"][0]
    assert esc_captured["dev"][0] == dev_model_ran

    # 3. The substrate-derived view agrees with the native records.
    conn = _sub.require_substrate(tmp_path)
    try:
        derived = _sub.derive_assignment_history(conn)
    finally:
        conn.close()
    assert [d["outcome"] for d in derived] == ["DONE", "ESCALATE"]

    # 4. No standalone assignment-history YAML surface is written by either run.
    assert not (tmp_path / ".forge" / "assignment_history.yaml").exists()

    # 5. The effect is visible in the NEXT run's routing_decision block: the dev
    #    candidate that ran twice now carries a two-run success-rate signal of
    #    0.5 (one DONE, one ESCALATE), consulted from the profiles both runs
    #    wrote. This is the escalation-history effect, read from the canonical
    #    block rather than a parallel trail.
    third_result, _ = _run_story(config, tmp_path)
    dev_block = third_result.state.routing_decision["dev"]
    dev_pool = _pool_by_name(dev_block)
    ran_as_dev = next(a.name for a in _agents() if a.model == dev_model_ran)
    signal = dev_pool[ran_as_dev]["signals"]["success_rate"]
    assert signal["runs"] == 2
    assert signal["raw"] == pytest.approx(0.5)
    # The promotion mechanism reports an outcome for that same evidence.
    assert dev_block["promotion_check"]["sample_size"] == 2
    assert dev_block["promotion_check"]["outcome"]


# ── AC7: canonical block completeness for a runtime path ──────────────


def test_persisted_block_is_complete_for_a_runtime_path(tmp_path):
    """The block persisted by a real coordinator run carries candidate pool,
    exclusion reasons, consulted signals, mechanism outcomes, and final
    selection for every role (ADR-0006 clause 7)."""
    config = _stable_dev_config(tmp_path)

    # A first run so the second one has capability profiles to consult — the
    # block must record the signals that were actually weighed, and a cold-start
    # run has none to record.
    first, _ = _run_story(config, tmp_path)
    _persist_run(first, config, tmp_path)

    result, captured = _run_story(config, tmp_path)
    record = _persist_run(result, config, tmp_path)

    # Persisted as a TOP-LEVEL key of the native record, not nested under preflight.
    block = record["routing_decision"]
    assert block is not None
    assert block["origin"] == "preflight"
    assert "excluded_for_taint" in block

    for role in ("preflight", "planner", "plan_review", "dev", "code_review"):
        role_block = block[role]
        pool = role_block["candidate_pool"]
        assert pool, f"{role} candidate pool must be reconstructable"
        for entry in pool:
            assert entry["reason"] in EXCLUSION_REASONS, (role, entry)
            if not entry["included"]:
                assert entry["reason"] != REASON_NONE, (role, entry)
        final = role_block["final"]
        assert ("model" in final) ^ ("models" in final)
        assert final["rationale"].startswith("[preflight]")

    # Consulted signals: the score policy the dev tier was read off, and the
    # per-candidate signal slots the router weighed.
    dev = block["dev"]
    assert dev["base_tier_from_score"] in ("cheap", "mid", "strong")
    assert dev["score_policy"]["dev_tier"]
    consulted = [e for e in dev["candidate_pool"] if "signals" in e]
    assert consulted, "the block must record the profile signals the router weighed"
    for entry in consulted:
        rate = entry["signals"]["success_rate"]
        for key in ("raw", "weighted", "runs", "floor"):
            assert key in rate, (entry["name"], key)

    # Adaptive mechanism outcomes — recorded even when they did not fire.
    assert dev["promotion_check"]["fired"] in (True, False)
    assert dev["promotion_check"]["outcome"]
    assert dev["demotion_check"]["mechanism"] == "dev_recency_recovery"
    assert dev["demotion_check"]["reason"]
    assert dev["post_plan_checkpoint"]["decision"]
    assert dev["routing_rationale"]["state"]

    # Final selection matches what the coordinator actually invoked.
    assert dev["final"]["model"] == captured["dev"][0]
    assert block["planner"]["final"]["model"] == captured.plan_model
    assert block["code_review"]["final"]["models"] == captured.code_review_models
    assert block["plan_review"]["final"]["models"] == captured.plan_review_models
