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
    reasons, consulted signals, adaptive-mechanism outcomes, and final selection;
  * the whole escalation-history learning arc — cold start below the sample
    floor, taint exclusion, promotion once the floor is met, sprint stickiness,
    and the paired recency recovery — holds end to end over one sprint against
    one substrate (#271, ADR-0006 clauses 2.3, 4, 5, 7).

Every explanation assertion reads from the canonical block; no parallel audit
trail is introduced.
"""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
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
    MECHANISM_DEV_PROMOTION,
    MECHANISM_DEV_RECENCY_RECOVERY,
    PROMOTION_OUTCOME_BELOW_FLOOR,
    PROMOTION_OUTCOME_PROMOTED,
    PROMOTION_OUTCOME_RECOVERED,
    REASON_EXPLICIT_OVERRIDE_LOCKED,
    REASON_NONE,
    ROUTING_RATIONALE_PROMOTED,
    ROUTING_RATIONALE_STAYED,
)
from theforge.config import (  # noqa: E402
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    AgentDef,
    AssignmentConfig,
    ExplorationConfig,
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
    load_escalation_history_with_taint_stats,
)
from theforge.coordinator.review_context import (  # noqa: E402
    REVIEWER_TREE_CURRENCY_CHECK,
    REVIEWER_TREE_CURRENCY_PRODUCER,
)
from theforge.coordinator.trust_status import (  # noqa: E402
    CHECK_FAIL,
    TRUST_TAINTED,
    make_trust_check,
)

PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""

# A failed reviewer tree-currency check — the landed mechanical producer of the
# trust marker (#1851). Injecting THIS (rather than hand-setting trust_status)
# keeps the tainted run in the fixture below on the production taint path: the
# coordinator derives ``trust_status: tainted`` from it, the audit writer records
# it, and the capability aggregator excludes the run (ADR-0006 clause 4).
_FAILED_TREE_CURRENCY_CHECK = make_trust_check(
    check=REVIEWER_TREE_CURRENCY_CHECK,
    result=CHECK_FAIL,
    producer=REVIEWER_TREE_CURRENCY_PRODUCER,
    evidence={
        "base_branch": "main",
        "expected_commits": ["deadbee handoff-claimed commit"],
        "actual_commits": [],
        "missing_from_branch": ["deadbee handoff-claimed commit"],
        "omitted_from_handoff": [],
    },
)

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
        plan=PlanConfig.of(
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
    sprint_name: str | None = None,
    taint: bool = False,
):
    """Drive a full ``run_task`` with only the agent boundary mocked.

    Returns ``(result, captured)`` where ``captured`` records the profiles the
    coordinator handed to each runner — the runtime truth the ACs are about.

    ``sprint_name`` threads the sprint context through so several stories can be
    run inside one sprint. ``taint`` forces the reviewer tree-currency trust
    check — the landed mechanical producer of the ``trust_status`` marker
    (#1851) — to FAIL, which is how a real run becomes ``tainted``; the taint
    then flows through the production paths into both the native record and the
    capability profiles.
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

    taint_patch = (
        patch(
            "theforge.coordinator.review_pool.evaluate_reviewer_tree_currency",
            return_value=_FAILED_TREE_CURRENCY_CHECK,
        )
        if taint
        else nullcontext()
    )

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
        taint_patch,
    ):
        result = run_task(config, task, sprint_name=sprint_name)
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


def test_persisted_run_uses_the_final_runtime_config_not_the_entry_config(tmp_path):
    """The production audit writer must persist the config the coordinator used."""
    config = _runtime_config(tmp_path)
    entry_dev_model = config.dev_profile.model

    result, captured = _run_story(config, tmp_path)
    record = _persist_run(result, config, tmp_path)

    assert captured["dev"][0] != entry_dev_model
    assert result.runtime_config is not None
    assert result.runtime_config.dev_profile.model == captured["dev"][0]
    assert (
        record["configuration"]["recorded_values"]["entries"]["dev_profile.model"]["value"]
        == captured["dev"][0]
    )
    assert (
        record["configuration"]["recorded_values"]["entries"]["dev_profile.model"]["value"]
        != entry_dev_model
    )


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
        plan=PlanConfig.of(
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
        plan_agent_review=PlanAgentReviewConfig.of(enabled=True, pool=[pinned]),
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


# ── #271: assignment-history learning end-to-end fixture ──────────────


_E2E_SPRINT = "adaptive-learning-e2e"
_TIER_LADDER = ("cheap", "mid", "strong")


def _learning_config(tmp_path: Path, **overrides) -> ForgeConfig:
    """Runtime config tuned for a short, deterministic learning arc.

    Only the sample floor moves off its production default: ``dev_promotion_min_runs``
    drops from 5 to 3 so the fixture can cross the floor in a handful of runs
    while still *having* a floor to cross. The promotion threshold, the recency
    weighting, and the taint gate are all left at their shipped values — the
    behaviour under test must be the production behaviour.
    """
    base = _stable_dev_config(tmp_path, **overrides)
    return _dc_replace(
        base,
        assignment=_dc_replace(
            base.assignment,
            dev_promotion_min_runs=3,
            # Challenger sampling (#325) is the other mechanism allowed to move
            # the dev pick off the static tier, and in winner mode it re-routes
            # to the empirical winner *after* a promotion fires. Disabling it
            # (per_sprint_cap=0) isolates the escalation-history arc under test;
            # exploration has its own coverage.
            exploration=ExplorationConfig(per_sprint_cap=0),
        ),
    )


def _dev_block(result) -> dict:
    return result.state.routing_decision["dev"]


def _legacy_yaml(tmp_path: Path) -> Path:
    return tmp_path / ".forge" / "assignment_history.yaml"


@pytest.mark.orchestration
def test_assignment_history_learning_end_to_end(tmp_path):
    """End-to-end proof of escalation-history learning through the substrate.

    One sprint, one complexity/dev-model slice, five phases:

      1. cold start — below the sample floor, routing falls back to the static
         tier/budget policy;
      2. accumulation — DONE and ESCALATE outcomes written as authoritative
         native telemetry, plus one tainted run;
      3. taint exclusion — the tainted run is visible in the substrate but keeps
         the admissible sample *below* the floor;
      4. promotion — once the floor is met the named mechanism fires and the
         routing_decision block records signal, sample, floor, taint exclusions
         and the ranking effect;
      5. recovery — as admissible successes return, the paired recency-recovery
         mechanism fires and the dev tier falls back to the static tier.

    Every assertion reads the native per-run records, the substrate projection,
    or the canonical ``routing_decision`` block. No assignment-history YAML is
    written or consulted anywhere in the arc (ADR-0006 clauses 2.3, 4, 5, 7).
    """
    config = _learning_config(tmp_path)
    min_runs = config.assignment.dev_promotion_min_runs
    threshold = config.assignment.dev_promotion_threshold

    # ── Phase 1: cold start ───────────────────────────────────────────
    # Empty history: no substrate, no capability profiles. The escalation-history
    # signal is consulted and comes back empty, so nothing crosses the floor and
    # the static score→tier policy decides.
    assert not _sub.substrate_path(tmp_path).exists()
    assert not (tmp_path / ".forge" / "model_profiles.yaml").exists()

    cold, cold_captured = _run_story(config, tmp_path, sprint_name=_E2E_SPRINT)
    assert cold.success is True
    cold_dev = _dev_block(cold)
    base_tier = cold_dev["base_tier_from_score"]
    assert base_tier in _TIER_LADDER
    # Consulted, but with nothing to consult: no signal, floor not passed.
    assert cold_dev["promotion_check"]["mechanism"] == MECHANISM_DEV_PROMOTION
    assert cold_dev["promotion_check"]["fired"] is False
    assert cold_dev["promotion_check"]["outcome"] == PROMOTION_OUTCOME_BELOW_FLOOR
    assert cold_dev["promotion_check"]["sample_size"] == 0
    assert cold_dev["promotion_check"]["floor"] == "fail"
    assert cold_dev["promotion_check"]["min_runs"] == min_runs
    # → fallback to the static tier/budget policy: the dev that RAN is the
    #   score-derived tier pick, unmoved by any adaptive mechanism.
    assert cold_dev["final"]["tier"] == base_tier
    assert cold_dev["routing_rationale"]["state"] == ROUTING_RATIONALE_STAYED
    assert cold_dev["routing_rationale"]["mechanism"] is None
    static_dev_model = cold_dev["final"]["model"]
    assert cold_captured["dev"] == [static_dev_model]
    assert cold.state.routing_decision["excluded_for_taint"] == 0
    assert not _legacy_yaml(tmp_path).exists()

    cold_record = _persist_run(cold, config, tmp_path)
    assert cold_record["outcome"]["success"] is True

    # ── Phase 2: authoritative DONE / ESCALATE / tainted telemetry ────
    # One admissible escalation. Still one run short of the floor at routing time.
    esc_one, _ = _run_story(
        config, tmp_path, sprint_name=_E2E_SPRINT, review_outputs=[REQUEST_CHANGES_REVIEW]
    )
    assert esc_one.phase.name == "ESCALATE"
    assert _dev_block(esc_one)["promotion_check"]["sample_size"] == 1
    esc_one_record = _persist_run(esc_one, config, tmp_path)
    assert esc_one_record["outcome"]["final_phase"] == "ESCALATE"

    # A tainted escalation for the SAME slice: the run happened, its telemetry is
    # real, but it failed its own trust check so it must not teach.
    tainted, _ = _run_story(
        config,
        tmp_path,
        sprint_name=_E2E_SPRINT,
        review_outputs=[REQUEST_CHANGES_REVIEW],
        taint=True,
    )
    assert tainted.phase.name == "ESCALATE"
    tainted_record = _persist_run(tainted, config, tmp_path)
    assert tainted_record["trust_status"] == TRUST_TAINTED
    assert [c["check"] for c in tainted_record["trust_checks"]] == [REVIEWER_TREE_CURRENCY_CHECK]

    # The second admissible escalation. Two admissible escalations now sit behind
    # this slice — and a third, tainted, run that must not count.
    esc_two, _ = _run_story(
        config, tmp_path, sprint_name=_E2E_SPRINT, review_outputs=[REQUEST_CHANGES_REVIEW]
    )
    assert esc_two.phase.name == "ESCALATE"
    _persist_run(esc_two, config, tmp_path)

    # ── Phase 3: the tainted run is present but excluded ──────────────
    # Present: the substrate still holds all four runs (ADR-0002 refusal to forget).
    conn = _sub.require_substrate(tmp_path)
    try:
        assert _sub.count_records(conn) == 4
        stored = _sub.latest_record_for(conn, run_id=tainted.state.run_id)
    finally:
        conn.close()
    assert stored is not None
    assert stored["trust_status"] == TRUST_TAINTED

    # Excluded: the router-consumed escalation history drops it and counts it.
    history, excluded_for_taint = load_escalation_history_with_taint_stats(tmp_path)
    assert excluded_for_taint == 1
    assert [r.outcome for r in history] == ["DONE", "ESCALATE", "ESCALATE"]
    # All three admissible runs are one slice: same complexity band, same dev
    # model identity (recorded canonically as provider/model/transport).
    assert {r.complexity for r in history} == {"MEDIUM"}
    history_dev_ids = {r.dev_model for r in history}
    assert len(history_dev_ids) == 1
    assert static_dev_model in next(iter(history_dev_ids))
    assert load_escalation_history_from_substrate(tmp_path) == history

    # Excluded from the routing-weight aggregate too: three admissible runs behind
    # the slice, the tainted one tallied separately — and that is exactly the
    # difference between meeting the floor and not.
    esc_two_promo = _dev_block(esc_two)["promotion_check"]
    assert esc_two_promo["sample_size"] == 2  # NOT 3 — the tainted run is out
    assert esc_two_promo["tainted_runs"] == 1
    assert esc_two_promo["outcome"] == PROMOTION_OUTCOME_BELOW_FLOOR
    assert esc_two_promo["floor"] == "fail"
    assert _dev_block(esc_two)["final"]["tier"] == base_tier  # still static
    assert esc_two.state.routing_decision["excluded_for_taint"] == 1

    # ── Phase 4: promotion once the admissible floor is met ───────────
    promoted, promoted_captured = _run_story(config, tmp_path, sprint_name=_E2E_SPRINT)
    promoted_dev = _dev_block(promoted)
    promo = promoted_dev["promotion_check"]

    # The named mechanism fired, on the named signal.
    assert promo["mechanism"] == MECHANISM_DEV_PROMOTION
    assert promo["fired"] is True
    assert promo["outcome"] == PROMOTION_OUTCOME_PROMOTED
    assert promo["model"] == cold.state._adaptive_decision.dev.name
    assert promo["complexity"] == "MEDIUM"
    # Consulted signal + sample count + floor status + taint exclusions.
    assert promo["sample_size"] == min_runs == 3  # DONE + 2 admissible ESCALATEs
    assert promo["tainted_runs"] == 1
    assert promo["floor"] == "pass"
    assert promo["threshold"] == threshold
    assert promo["raw_success_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert promo["weighted_success_rate"] < threshold
    # Final ranking effect: one tier up from the static pick, and that is the
    # model the coordinator actually invoked.
    promoted_tier = promo["resulting_tier"]
    assert _TIER_LADDER.index(promoted_tier) == _TIER_LADDER.index(base_tier) + 1
    assert promoted_dev["final"]["tier"] == promoted_tier
    assert promoted_dev["final"]["model"] != static_dev_model
    assert promoted_captured["dev"] == [promoted_dev["final"]["model"]]
    assert promoted_dev["routing_rationale"] == {
        "state": ROUTING_RATIONALE_PROMOTED,
        "mechanism": MECHANISM_DEV_PROMOTION,
        "from_tier": base_tier,
        "to_tier": promoted_tier,
    }
    # The paired recovery mechanism is recorded as checked-but-not-fired: the
    # promotion is active because the weighted rate has not come back yet.
    assert promoted_dev["demotion_check"]["mechanism"] == MECHANISM_DEV_RECENCY_RECOVERY
    assert promoted_dev["demotion_check"]["applicable"] is True
    assert promoted_dev["demotion_check"]["fired"] is False
    assert (
        promoted_dev["demotion_check"]["reason"]
        == "promotion_active_weighted_rate_below_threshold"
    )

    # The whole explanation is reconstructable from the persisted native record.
    promoted_record = _persist_run(promoted, config, tmp_path)
    assert promoted_record["routing_decision"]["dev"]["promotion_check"] == promo
    assert promoted_record["routing_decision"]["excluded_for_taint"] == 1
    assert not _legacy_yaml(tmp_path).exists()

    # ── Phase 4b: sprint stickiness ───────────────────────────────────
    # The next story in the SAME sprint re-derives the identical decision from
    # the same native records. Stickiness is a property of the shared substrate,
    # not of a second store: no promotion cache and no assignment-history file
    # exists for it to have been read from.
    sticky, sticky_captured = _run_story(config, tmp_path, sprint_name=_E2E_SPRINT)
    sticky_dev = _dev_block(sticky)
    assert sticky_dev["promotion_check"] == promo
    assert sticky_dev["final"] == promoted_dev["final"]
    assert sticky_captured["dev"] == promoted_captured["dev"]
    assert sticky.state.sprint_name == _E2E_SPRINT == promoted.state.sprint_name
    assert sticky.state.routing_decision["excluded_for_taint"] == 1
    assert not _legacy_yaml(tmp_path).exists()
    _persist_run(sticky, config, tmp_path)

    # ── Phase 5: recovery ─────────────────────────────────────────────
    # Successes return to the promoted-away-from slice. Pin dev to that exact
    # profile so the recovering evidence lands on the same (complexity, dev_model)
    # bucket the promotion was read off — an operator override, not a test-only
    # back door into the aggregates.
    pinned_config = _learning_config(tmp_path, dev_profile=cold.state._adaptive_decision.dev)
    for _ in range(3):
        recovering, recovering_captured = _run_story(
            pinned_config, tmp_path, sprint_name=_E2E_SPRINT
        )
        assert recovering.success is True
        assert recovering_captured["dev"] == [static_dev_model]
        _persist_run(recovering, pinned_config, tmp_path)

    recovered, recovered_captured = _run_story(config, tmp_path, sprint_name=_E2E_SPRINT)
    recovered_dev = _dev_block(recovered)
    recovered_promo = recovered_dev["promotion_check"]

    # Admissible samples remain (still one taint-excluded) and the weighted rate
    # has climbed back to/above threshold: the promotion stops firing...
    assert recovered_promo["fired"] is False
    assert recovered_promo["outcome"] == PROMOTION_OUTCOME_RECOVERED
    assert recovered_promo["floor"] == "pass"
    assert recovered_promo["sample_size"] == 6
    assert recovered_promo["tainted_runs"] == 1
    assert recovered_promo["weighted_success_rate"] >= threshold
    # ...and the paired recovery mechanism records the return path firing.
    assert recovered_dev["demotion_check"]["mechanism"] == MECHANISM_DEV_RECENCY_RECOVERY
    assert recovered_dev["demotion_check"]["applicable"] is True
    assert recovered_dev["demotion_check"]["fired"] is True
    assert (
        recovered_dev["demotion_check"]["reason"]
        == "weighted_rate_recovered_to_or_above_threshold"
    )
    # Ranking effect of the recovery: dev is back at the static tier.
    assert recovered_dev["final"]["tier"] == base_tier
    assert recovered_dev["final"]["model"] == static_dev_model
    assert recovered_captured["dev"] == [static_dev_model]
    assert recovered_dev["routing_rationale"]["state"] == ROUTING_RATIONALE_STAYED

    # ── Final record assertions target native surfaces only ───────────
    recovered_record = _persist_run(recovered, config, tmp_path)
    assert recovered_record["routing_decision"]["dev"]["demotion_check"]["fired"] is True
    runs_dir = tmp_path / ".forge" / "audits" / "runs"
    assert len(list(runs_dir.glob("*.json"))) == 10
    conn = _sub.require_substrate(tmp_path)
    try:
        derived = _sub.derive_assignment_history(conn)
    finally:
        conn.close()
    # The derived view is a projection of the native records with the tainted run
    # filtered out — nine admissible runs, no separate authority behind them.
    assert len(derived) == 9
    assert not _legacy_yaml(tmp_path).exists()
    assert not (tmp_path / ".forge" / "assignment_history.yml").exists()
