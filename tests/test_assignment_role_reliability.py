"""Non-dev adaptive routing on role reliability signals (#1489).

Extends the reviewer completion-rate pattern (#1388) to the two single-model
non-dev roles — preflight and planner. A role whose recency-weighted attempt-
completion rate is below the floor, once it has enough admissible attempts, is
sorted *after* more-reliable candidates: a sort-after, never a filter-out, and
always subordinate to explicit operator configuration (ADR-0006 clauses 1/2).

Covers the model_profiles folding/signal machinery, the assignment reranking, the
routing_decision audit, and a coordinator-bridge seam roundtrip.
"""

from __future__ import annotations

import pytest

from theforge.assignment import AssignmentConfig, assign_models
from theforge.config import AgentDef, ModelProfile
from theforge.model_profiles import (
    RoleAttempt,
    RunOutcome,
    apply_run,
    get_role_reliability_signal,
)


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")


# ── Fixtures ───────────────────────────────────────────────────────────────


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        max_cost_per_story_usd=1000.0,
        reviewer_completion_threshold=0.5,
        reviewer_completion_min_runs=5,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _agents() -> list[AgentDef]:
    # Two cheap (preflight tier) and two strong (planner tier at MEDIUM), same
    # budget within a tier so — absent reliability — the stable order is input
    # order (…-a before …-b).
    return [
        AgentDef("pf-a", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("pf-b", "openai", "gpt-mini", 1.0, 300, "cheap"),
        AgentDef("pl-a", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("pl-b", "openai", "gpt-strong", 8.0, 1200, "strong"),
    ]


def _reliability_entry(role: str, attempted: int, completed: int) -> dict:
    # All-completed outcomes first, then all-failed, so the recency-weighted rate
    # for a poor model leans on its recent failures (unambiguously below floor).
    return {
        role: {
            "_attempted_count": attempted,
            "_completed_count": completed,
            "completion_rate": round(completed / attempted, 4) if attempted else 0.0,
            "_completion_recent": [1] * completed + [0] * (attempted - completed),
        }
    }


def _profiles(**by_name: dict) -> dict:
    return {"models": dict(by_name)}


# ── model_profiles: folding + signal ────────────────────────────────────────


def test_apply_run_folds_preflight_and_planner_completion():
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="medium",
        dev_model="dev",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.0,
        preflight_model="pf-a",
        preflight_actual_model="haiku",
        preflight_provider="anthropic",
        preflight_cost_usd=0.01,
        preflight_attempts=[
            RoleAttempt("pf-a", completed=False, actual_model="haiku", provider="anthropic")
        ],
        planner_model="pl-a",
        planner_actual_model="opus",
        planner_provider="anthropic",
        planner_cost_usd=0.02,
        planner_attempts=[
            RoleAttempt("pl-a", completed=True, actual_model="opus", provider="anthropic")
        ],
    )
    apply_run(data, outcome)

    pf = data["models"]["anthropic/haiku/api"]["preflight"]
    assert pf["_attempted_count"] == 1
    assert pf["_completed_count"] == 0
    assert pf["completion_rate"] == 0.0
    pl = data["models"]["anthropic/opus/api"]["planner"]
    assert pl["_attempted_count"] == 1
    assert pl["_completed_count"] == 1
    assert pl["completion_rate"] == 1.0


def test_preflight_retry_then_fallback_attributes_per_attempt():
    # A failed primary + successful fallback must record a FAILURE for the primary
    # and a SUCCESS for the fallback — never a spurious primary success (#1489 P1).
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="medium",
        dev_model="dev",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.0,
        preflight_model="pf-a",
        preflight_actual_model="haiku",
        preflight_provider="anthropic",
        preflight_cost_usd=0.01,
        preflight_attempts=[
            RoleAttempt("pf-a", completed=False, actual_model="haiku", provider="anthropic"),
            RoleAttempt("pf-b", completed=True, actual_model="gpt-mini", provider="openai"),
        ],
    )
    apply_run(data, outcome)
    primary = data["models"]["anthropic/haiku/api"]["preflight"]
    assert primary["_attempted_count"] == 1
    assert primary["_completed_count"] == 0
    fallback = data["models"]["openai/gpt-mini/api"]["preflight"]
    assert fallback["_attempted_count"] == 1
    assert fallback["_completed_count"] == 1


def test_planner_transport_retry_folds_failed_attempt():
    # A transport-retry failure followed by a successful plan output must still
    # record the failed attempt — not be hidden by the later success (#1489).
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="medium",
        dev_model="dev",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.0,
        planner_model="pl-a",
        planner_actual_model="opus",
        planner_provider="anthropic",
        planner_cost_usd=0.02,
        planner_attempts=[
            RoleAttempt("pl-a", completed=False, actual_model="opus", provider="anthropic"),
            RoleAttempt("pl-a", completed=True, actual_model="opus", provider="anthropic"),
        ],
    )
    apply_run(data, outcome)
    pl = data["models"]["anthropic/opus/api"]["planner"]
    assert pl["_attempted_count"] == 2
    assert pl["_completed_count"] == 1
    assert pl["completion_rate"] == 0.5


def test_no_preflight_attempts_records_no_completion():
    # A cached preflight (no attempts) still records runs/cost but folds no
    # completion attempt, so the reliability signal stays cold-start.
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="medium",
        dev_model="dev",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.0,
        preflight_model="pf-a",
        preflight_actual_model="haiku",
        preflight_provider="anthropic",
        preflight_cost_usd=0.01,
        preflight_attempts=[],
    )
    apply_run(data, outcome)
    pf = data["models"]["anthropic/haiku/api"]["preflight"]
    assert pf["runs"] == 1
    assert "_attempted_count" not in pf


def test_role_reliability_signal_excludes_tainted_runs():
    # A tainted run doesn't teach: it is tallied under tainted_runs and never
    # folded into the completion aggregate (ADR-0006 clause 4, #1852).
    data: dict = {"models": {}}
    for _ in range(3):
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model="dev",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=0.0,
                preflight_model="pf-a",
                preflight_actual_model="haiku",
                preflight_provider="anthropic",
                preflight_cost_usd=0.0,
                preflight_attempts=[
                    RoleAttempt("pf-a", completed=True, actual_model="haiku", provider="anthropic")
                ],
                dev_tainted=True,
            ),
        )
    sig = get_role_reliability_signal(
        data, "pf-a", "preflight", 1, actual_model="haiku", provider="anthropic"
    )
    assert sig["attempted"] == 0
    assert sig["tainted_runs"] == 3
    assert sig["floor"] == "fail"
    assert sig["rate"] is None


def test_role_reliability_signal_cold_start_below_floor():
    profiles = _profiles(**{"pf-a": _reliability_entry("preflight", 3, 0)})
    sig = get_role_reliability_signal(profiles, "pf-a", "preflight", 5)
    assert sig["floor"] == "fail"
    assert sig["rate"] is None  # below floor → no ranking weight
    assert sig["raw"] == 0.0  # raw still visible for the audit


def test_role_reliability_signal_schema_incompatible_forces_cold_start():
    # A legacy preflight section with runs but no completion counters cannot answer
    # the reliability question — it must read as schema-incompatible cold start,
    # never as 0% completion.
    profiles = _profiles(**{"pf-a": {"preflight": {"runs": 20, "avg_cost_usd": 0.01}}})
    sig = get_role_reliability_signal(profiles, "pf-a", "preflight", 5)
    assert sig["schema_ok"] is False
    assert sig["floor"] == "fail"
    assert sig["rate"] is None


# ── assignment: reranking ───────────────────────────────────────────────────


def test_preflight_low_reliability_reorders_selection():
    # pf-a would win on stable order, but 10 attempts at 10% completion (< 0.5
    # floor) sort it after the reliable pf-b.
    profiles = _profiles(
        **{
            "pf-a": _reliability_entry("preflight", 10, 1),
            "pf-b": _reliability_entry("preflight", 10, 10),
        }
    )
    decision = assign_models(_agents(), _cfg(), "medium", model_profiles=profiles)
    assert decision.preflight.name == "pf-b"


def test_preflight_cold_start_preserves_static_order():
    # Same poor pf-a rate but only 3 attempts (< min_runs 5): cold start, so the
    # static budget/order pick (pf-a) stands.
    profiles = _profiles(
        **{
            "pf-a": _reliability_entry("preflight", 3, 0),
            "pf-b": _reliability_entry("preflight", 3, 3),
        }
    )
    decision = assign_models(_agents(), _cfg(), "medium", model_profiles=profiles)
    assert decision.preflight.name == "pf-a"


def test_planner_low_reliability_reorders_selection():
    profiles = _profiles(
        **{
            "pl-a": _reliability_entry("planner", 8, 1),
            "pl-b": _reliability_entry("planner", 8, 8),
        }
    )
    decision = assign_models(_agents(), _cfg(), "medium", model_profiles=profiles)
    assert decision.planner.name == "pl-b"


def test_low_reliability_is_sort_after_not_filter_out():
    # When the ONLY cheap candidate is unreliable it is still selected — a
    # sort-after never locks an operator-enabled agent out of the pool.
    agents = [AgentDef("pf-a", "anthropic", "haiku", 1.0, 300, "cheap")]
    # Add a strong agent so planner/reviewer roles still resolve.
    agents.append(AgentDef("pl-a", "anthropic", "opus", 8.0, 1200, "strong"))
    profiles = _profiles(**{"pf-a": _reliability_entry("preflight", 10, 0)})
    decision = assign_models(agents, _cfg(), "medium", model_profiles=profiles)
    assert decision.preflight.name == "pf-a"


def test_explicit_preflight_override_beats_learned_preference():
    # Operator pins preflight to the unreliable model; learned reliability must not
    # override explicit configuration (ADR-0006 clause 1).
    profiles = _profiles(
        **{
            "pf-a": _reliability_entry("preflight", 10, 1),
            "pf-b": _reliability_entry("preflight", 10, 10),
        }
    )
    explicit_pf = ModelProfile(
        name="pinned-preflight",
        cli="claude",
        provider=None,
        model="pf-a-model",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Grep"),
    )
    decision = assign_models(
        _agents(),
        _cfg(),
        "medium",
        model_profiles=profiles,
        explicit_profiles={"preflight": explicit_pf},
    )
    assert decision.preflight.model == "pf-a-model"
    # The reliability rerank never ran for an overridden role.
    assert "reliability_check" not in decision.routing_decision["preflight"]


def test_static_mode_ignores_role_reliability():
    profiles = _profiles(
        **{
            "pf-a": _reliability_entry("preflight", 10, 1),
            "pf-b": _reliability_entry("preflight", 10, 10),
        }
    )
    decision = assign_models(
        _agents(), _cfg(adaptive_enabled=False), "medium", model_profiles=profiles
    )
    # Static band-only routing ignores profile learning: input order (pf-a) holds.
    assert decision.preflight.name == "pf-a"
    assert "reliability_check" not in decision.routing_decision["preflight"]


# ── assignment: routing_decision audit ──────────────────────────────────────


def test_preflight_reliability_audit_records_signal_and_effect():
    profiles = _profiles(
        **{
            "pf-a": _reliability_entry("preflight", 10, 1),
            "pf-b": _reliability_entry("preflight", 10, 10),
        }
    )
    decision = assign_models(_agents(), _cfg(), "medium", model_profiles=profiles)
    check = decision.routing_decision["preflight"]["reliability_check"]
    assert check["mechanism"] == "role_reliability"
    assert check["fired"] is True
    assert check["threshold"] == 0.5
    assert check["min_runs"] == 5
    assert check["deprioritized"] == ["pf-a"]
    assert check["original_order"] == ["pf-a", "pf-b"]
    assert check["final_order"] == ["pf-b", "pf-a"]
    # Per-candidate consulted signal: counts, floor, weighted/raw, taint, selection.
    pf_a = check["signals"]["pf-a"]
    assert pf_a["attempted"] == 10
    assert pf_a["floor"] == "pass"
    assert pf_a["rate"] is not None
    assert pf_a["tainted_runs"] == 0
    assert pf_a["selected"] is False
    assert check["signals"]["pf-b"]["selected"] is True


def test_cold_start_audit_shows_no_demotion():
    profiles = _profiles(
        **{
            "pf-a": _reliability_entry("preflight", 3, 0),
            "pf-b": _reliability_entry("preflight", 3, 3),
        }
    )
    decision = assign_models(_agents(), _cfg(), "medium", model_profiles=profiles)
    check = decision.routing_decision["preflight"]["reliability_check"]
    assert check["fired"] is False
    assert check["deprioritized"] == []
    assert check["signals"]["pf-a"]["floor"] == "fail"


# ── seam: bridge → apply_run → signal roundtrip ─────────────────────────────


def test_bridge_roundtrip_teaches_role_reliability(monkeypatch):
    # A poor preflight run recorded by the coordinator bridge must, after folding,
    # move the next preflight decision — proving the telemetry the bridge derives
    # is admissible-signal shaped end to end (convention 8 seam coverage).
    data: dict = {"models": {}}
    # 6 preflight failures for pf-a → recency-weighted completion ~0.
    for _ in range(6):
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model="dev",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=0.0,
                preflight_model="pf-a",
                preflight_actual_model="haiku",
                preflight_provider="anthropic",
                preflight_cost_usd=0.0,
                preflight_attempts=[
                    RoleAttempt(
                        "pf-a", completed=False, actual_model="haiku", provider="anthropic"
                    )
                ],
            ),
        )
    sig = get_role_reliability_signal(
        data, "pf-a", "preflight", 5, actual_model="haiku", provider="anthropic"
    )
    assert sig["floor"] == "pass"
    assert sig["rate"] is not None and sig["rate"] < 0.5

    decision = assign_models(_agents(), _cfg(), "medium", model_profiles=data)
    assert decision.preflight.name == "pf-b"
