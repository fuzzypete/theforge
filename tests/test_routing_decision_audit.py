"""Seam-level reconstructability test for the routing_decision block (#1391).

Exercises the assignment → audit seam: assign_models produces the structured
routing_decision, the coordinator carries it on run state, and generate_audit_log
persists it as a TOP-LEVEL key in the authoritative native per-run record. The
test then reconstructs the whole routing decision from that block alone — every
candidate's inclusion/exclusion + reason, every consulted signal's values, every
adaptive mechanism checked and its outcome, exploration mode, and final
selection. A decision the block cannot fully explain fails the test (ADR-0006
clause 7).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.assignment import (
    EXCLUSION_REASONS,
    REASON_ANTI_SELF_REVIEW,
    REASON_AUTH_MISSING,
    AssignmentConfig,
    assign_models,
)
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    AgentDef,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.task import TaskStory


@pytest.fixture()
def _keys_except_deepseek(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="gpt",
            provider="openai",
            model="gpt-5.4",
            budget_usd=8.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="deepseek",
            provider="deepseek",
            model="deepseek-reasoner",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="strong",
        ),
    ]


def _cfg() -> AssignmentConfig:
    return AssignmentConfig(
        enabled=True,
        min_reviewers=2,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=100.0,
        escalation_memory=True,
    )


def _make_config(tmp_path: Path) -> ForgeConfig:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec", encoding="utf-8")
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(gate_command="make gate"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _record_through_seam(tmp_path: Path, profiles: dict | None = None) -> dict:
    """Run assign_models, carry the block on state, and emit the audit record."""
    profiles = profiles or {
        "models": {
            "opus": {
                "dev": {
                    "runs": 12,
                    "success_rate": 0.8,
                    "by_complexity": {"large": {"runs": 12, "success_rate": 0.8}},
                }
            },
        }
    }
    decision = assign_models(
        _agents(), _cfg(), complexity="HIGH", complexity_score=9, model_profiles=profiles
    )

    config = _make_config(tmp_path)
    task = TaskStory(name="Test", slug="test", story_path=tmp_path / "spec.md")
    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    state.preflight_verdict = "PROCEED"
    state.preflight_complexity = "HIGH"
    state.preflight_complexity_score = 9
    # The coordinator carries the structured block onto run state (preflight.py).
    state.routing_decision = decision.routing_decision
    result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")
    return generate_audit_log(config, task, result)


def test_routing_decision_is_a_top_level_audit_key(tmp_path, _keys_except_deepseek):
    record = _record_through_seam(tmp_path)
    # Top-level, NOT nested under preflight.complexity_routing (ADR-0002 §1-2).
    assert "routing_decision" in record
    assert record["routing_decision"] is not None
    assert set(record["routing_decision"]).issuperset(
        {"preflight", "planner", "plan_review", "dev", "code_review"}
    )


def test_preexisting_audit_fields_unchanged(tmp_path, _keys_except_deepseek):
    record = _record_through_seam(tmp_path)
    # The block is purely additive: legacy cost shape is intact, and the
    # outcome-only preflight.complexity_routing view is NOT replaced by the new
    # top-level block (the two coexist per ADR-0006 clause 7). The full field-set
    # guard lives in tests/test_audit_schema_guard.py.
    assert isinstance(record["cost"]["agents"], list)
    assert "dev_usd" in record["cost"]
    assert record["routing_decision"] is not record["preflight"].get("complexity_routing")


def test_full_reconstruction_from_block_alone(tmp_path, _keys_except_deepseek):
    """Reconstruct the entire routing decision using ONLY routing_decision."""
    record = _record_through_seam(tmp_path)
    block = record["routing_decision"]

    # Origin is labeled so a future checkpoint origin can coexist.
    assert block["origin"] == "preflight"

    for role in ("preflight", "planner", "plan_review", "dev", "code_review"):
        role_block = block[role]
        pool = role_block["candidate_pool"]
        assert pool, f"{role} candidate pool must be reconstructable"

        # Every candidate is either included or carries a canonical exclusion reason.
        for entry in pool:
            assert "name" in entry and "included" in entry
            assert entry["reason"] in EXCLUSION_REASONS
            if not entry["included"]:
                assert entry["reason"] != "none"

        # Exploration mode is reconstructable for every role.
        assert role_block["exploration"]["mode"] in ("winner", "challenger")

        # Final selection is reconstructable (single-model vs reviewer shape).
        final = role_block["final"]
        assert "rationale" in final and final["rationale"].startswith("[preflight]")
        assert ("model" in final) ^ ("models" in final)

    # Dev-specific adaptive mechanisms are all reconstructable, each with an
    # outcome — including the ones that did NOT fire.
    dev = block["dev"]
    assert dev["promotion_check"]["fired"] in (True, False)
    assert "outcome" in dev["promotion_check"]
    # Demotion mechanism does not exist in v1: "no such mechanism ran" is a
    # COMPLETE explanation, not a gap.
    assert dev["demotion_check"]["applicable"] is False
    assert dev["demotion_check"]["reason"]
    assert dev["post_plan_checkpoint"]["applied"] is False
    assert dev["post_plan_checkpoint"]["reason"]
    assert dev["base_tier_from_score"] in ("cheap", "mid", "strong")

    # The signals the router weighed are reconstructable with raw/weighted/runs/floor.
    dev_pool = {e["name"]: e for e in dev["candidate_pool"]}
    opus_sig = dev_pool["opus"]["signals"]["success_rate"]
    assert opus_sig["raw"] == 0.8
    assert opus_sig["weighted"] == 0.8
    assert opus_sig["runs"] == 12
    assert opus_sig["floor"] == "pass"

    # The final selected dev model is itself an included candidate in the pool —
    # the block is internally consistent and self-contained.
    assert (
        dev_pool[next(a.name for a in _agents() if a.model == dev["final"]["model"])]["included"]
        is True
    )


def test_required_case_anti_self_review_and_auth_missing(tmp_path, _keys_except_deepseek):
    """The mandated case: one model excluded by anti_self_review, another by
    auth_missing, both attributed to the expected model in the block alone."""
    record = _record_through_seam(tmp_path)
    pool = {e["name"]: e for e in record["routing_decision"]["code_review"]["candidate_pool"]}

    # Reconstruct the dev model purely from the block's dev final selection.
    dev_model = record["routing_decision"]["dev"]["final"]["model"]

    anti_self = [
        n for n, e in pool.items() if not e["included"] and e["reason"] == REASON_ANTI_SELF_REVIEW
    ]
    assert anti_self, "expected an anti_self_review exclusion"
    for name in anti_self:
        agent = next(a for a in _agents() if a.name == name)
        assert agent.model == dev_model  # attributed to the model that ran as dev

    assert pool["deepseek"]["included"] is False
    assert pool["deepseek"]["reason"] == REASON_AUTH_MISSING
