"""Code-review reviewer-value routing (#2156).

Extends the plan-review mechanical value signal (#1443) to the code-review
reviewer selection: a reviewer whose blocking-finding uniqueness at the story's
complexity band is below threshold — over at least the sample floor of
admissible code-review samples — is sorted after higher-value candidates, while a
reviewer that merely *answers* reliably no longer rides its completion rate into
the pool.

Both directions of the routing-symmetry pairing are exercised here: the
``value_check`` deprioritization and its registered inverse, the passive
``recovery_check`` recency recovery that stops the deprioritization firing once a
reviewer's recent contribution comes back.
"""

from __future__ import annotations

import pytest

from theforge.assignment import (
    MECHANISM_REVIEWER_VALUE_DEPRIORITIZE,
    MECHANISM_REVIEWER_VALUE_RECOVERY,
    ROUTING_SYMMETRY_REGISTRY,
    AssignmentConfig,
    assign_models,
)
from theforge.config import AgentDef
from theforge.model_profiles import RunOutcome, apply_run
from theforge.reviewer_value import (
    CODE_PHASE,
    CODE_SECTION,
    PLAN_PHASE,
    SECTION,
    ReviewerValueSample,
    code_review_anchor_text,
    compute_reviewer_uniqueness,
    fold_reviewer_value,
    get_reviewer_value_signal,
)


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")


# ── Fixtures ───────────────────────────────────────────────────────────────


def _agents() -> list[AgentDef]:
    # A cheap dev model keeps both strong reviewers out of the anti-self-review
    # exclusion, so ordering is driven purely by the value signal.
    return [
        AgentDef("dev-cheap", "deepseek", "ds", 1.0, 300, "cheap"),
        AgentDef("rev-a", "anthropic", "sonnet", 5.0, 600, "strong"),
        AgentDef("rev-b", "openai", "gpt", 5.0, 600, "strong"),
    ]


def _completion(attempted: int, completed: int) -> dict:
    """A reviewer-completion record: this is the signal that governs today."""
    return {
        "_attempted_count": attempted,
        "_completed_count": completed,
        "completion_rate": round(completed / attempted, 4) if attempted else 0.0,
        "_completion_recent": [1] * completed + [0] * (attempted - completed),
    }


def _value_bucket(ring: list[float], latency_per_p1: float = 60.0) -> dict:
    runs = len(ring)
    return {
        "runs": runs,
        "tainted_runs": 0,
        "_uniqueness_sum": sum(ring),
        "_latency_per_p1_sum": latency_per_p1 * runs,
        "avg_uniqueness_rate": round(sum(ring) / runs, 4) if runs else 0.0,
        "avg_latency_per_p1": latency_per_p1,
        "_uniqueness_recent": list(ring),
        "_latency_per_p1_recent": [latency_per_p1] * runs,
    }


def _entry(
    provider: str,
    model: str,
    *,
    attempted: int,
    completed: int,
    code_ring: list[float] | None = None,
    plan_ring: list[float] | None = None,
    band: str = "small",
) -> dict:
    """A profile entry stamped the way the real fold path (_ensure_model) does."""
    entry: dict = {
        "_identity": {"provider": provider, "model": model, "transport": "api"},
        "review": _completion(attempted, completed),
    }
    for section, ring in ((CODE_SECTION, code_ring), (SECTION, plan_ring)):
        if ring is None:
            continue
        bucket = _value_bucket(ring)
        entry[section] = {**bucket, "by_complexity": {band: _value_bucket(ring)}}
    return entry


def _profiles(**by_key: dict) -> dict:
    return {"models": dict(by_key)}


def _cfg(**overrides) -> AssignmentConfig:
    base = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        max_cost_per_story_usd=100.0,
        escalation_memory=False,
        code_review_value_enabled=True,
        code_review_value_uniqueness_threshold=0.34,
        code_review_value_min_runs=5,
    )
    base.update(overrides)
    return AssignmentConfig(**base)


# ── The story's headline case ──────────────────────────────────────────────


def test_reliable_completer_with_no_unique_findings_is_deprioritized():
    """The spec's example: answers reliably, contributes nothing unique.

    rev-a completes 9 of 10 code reviews (it always *responds*) but over 8
    admissible samples raised no blocking finding a peer had not already raised.
    rev-b answers less often but its blocking findings are its own. Today only
    completion is consulted and rev-a is seated; with the value signal wired in,
    rev-b is.
    """
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _entry(
                "anthropic", "sonnet", attempted=10, completed=9, code_ring=[0.0] * 8
            ),
            "openai/gpt/api": _entry(
                "openai", "gpt", attempted=10, completed=7, code_ring=[1.0] * 8
            ),
        }
    )
    decision = assign_models(_agents(), _cfg(), complexity="LOW", model_profiles=profiles)

    assert [p.name for p in decision.code_reviewers] == ["rev-b"]
    value = decision.routing_decision["code_review"]["value_check"]
    assert value["mechanism"] == MECHANISM_REVIEWER_VALUE_DEPRIORITIZE
    assert value["phase"] == "code_review"
    assert value["fired"] is True
    assert value["deprioritized"] == ["rev-a"]
    # The uniqueness figures and the sample count that justified it are recorded
    # per reviewer, per complexity band.
    assert value["complexity"] == "LOW"  # normalised to the "small" band on read
    assert value["min_runs"] == 5
    assert value["signals"]["rev-a"]["runs"] == 8
    assert value["signals"]["rev-a"]["uniqueness_rate"]["floor"] == "pass"
    assert value["signals"]["rev-a"]["uniqueness_rate"]["rate"] == 0.0
    assert value["signals"]["rev-b"]["uniqueness_rate"]["rate"] == 1.0
    assert value["signals"]["rev-a"]["latency_per_p1"]["rate"] == 60.0
    # And the human-readable rationale says the check fired, not just the audit.
    assert "value check: deprioritized rev-a" in decision.rationale["code_review"]


def test_evaluated_but_not_fired_is_recorded():
    """Both reviewers clear the threshold → checked, did not fire, order intact."""
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _entry(
                "anthropic", "sonnet", attempted=10, completed=9, code_ring=[1.0] * 8
            ),
            "openai/gpt/api": _entry(
                "openai", "gpt", attempted=10, completed=9, code_ring=[1.0] * 8
            ),
        }
    )
    decision = assign_models(_agents(), _cfg(), complexity="LOW", model_profiles=profiles)

    assert [p.name for p in decision.code_reviewers] == ["rev-a"]
    value = decision.routing_decision["code_review"]["value_check"]
    assert value["fired"] is False
    assert value["deprioritized"] == []
    assert set(value["signals"]) == {"rev-a", "rev-b"}
    assert "none below the uniqueness threshold" in decision.rationale["code_review"]


# ── Sample floor: cold start falls through ─────────────────────────────────


def test_below_sample_floor_falls_through_to_completion_behaviour():
    """A newly enabled reviewer is not deprioritized for absence of history.

    rev-a has zero unique findings but only 3 code-review samples (< the floor of
    5), so the value check must not fire and selection falls through to the
    existing completion-rate behaviour — which seats rev-a.
    """
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _entry(
                "anthropic", "sonnet", attempted=10, completed=10, code_ring=[0.0] * 3
            ),
            "openai/gpt/api": _entry(
                "openai", "gpt", attempted=10, completed=10, code_ring=[1.0] * 3
            ),
        }
    )
    decision = assign_models(_agents(), _cfg(), complexity="LOW", model_profiles=profiles)

    assert [p.name for p in decision.code_reviewers] == ["rev-a"]
    value = decision.routing_decision["code_review"]["value_check"]
    assert value["fired"] is False
    assert value["deprioritized"] == []
    assert value["signals"]["rev-a"]["uniqueness_rate"]["floor"] == "fail"
    assert value["signals"]["rev-a"]["uniqueness_rate"]["rate"] is None
    assert "below the 5-sample floor" in decision.rationale["code_review"]


def test_disabled_by_default_omits_the_block_entirely():
    """Opt-in: with the flag off, code-review selection is exactly as before."""
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _entry(
                "anthropic", "sonnet", attempted=10, completed=10, code_ring=[0.0] * 8
            ),
            "openai/gpt/api": _entry(
                "openai", "gpt", attempted=10, completed=10, code_ring=[1.0] * 8
            ),
        }
    )
    cfg = _cfg(code_review_value_enabled=False)
    decision = assign_models(_agents(), cfg, complexity="LOW", model_profiles=profiles)

    assert [p.name for p in decision.code_reviewers] == ["rev-a"]
    assert "value_check" not in decision.routing_decision["code_review"]
    assert "value check" not in decision.rationale["code_review"]


def test_plan_review_value_does_not_deprioritize_for_code_review():
    """The two sections are independent: a poor plan-review record is not carried over.

    rev-a has an all-zero *plan-review* uniqueness history over the floor and no
    code-review history at all. Code-review selection must ignore it entirely.
    """
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _entry(
                "anthropic", "sonnet", attempted=10, completed=10, plan_ring=[0.0] * 8
            ),
            "openai/gpt/api": _entry(
                "openai", "gpt", attempted=10, completed=10, plan_ring=[1.0] * 8
            ),
        }
    )
    decision = assign_models(_agents(), _cfg(), complexity="LOW", model_profiles=profiles)

    assert [p.name for p in decision.code_reviewers] == ["rev-a"]
    value = decision.routing_decision["code_review"]["value_check"]
    assert value["fired"] is False
    assert value["signals"]["rev-a"]["runs"] == 0
    assert value["signals"]["rev-a"]["uniqueness_rate"]["floor"] == "fail"


# ── Recovery: the registered inverse ───────────────────────────────────────


def test_recovered_reviewer_is_not_deprioritized_and_recovery_is_recorded():
    """A reviewer whose contribution comes back stops being deprioritized.

    rev-a's lifetime uniqueness is 0.25 (below the 0.34 threshold) but its recent
    samples are all unique, so the recency-weighted rate the router consults is
    back above threshold. The deprioritization does not fire and the passive
    recovery is recorded as the registered inverse.
    """
    # 50 stale zero-uniqueness samples, then 20 fresh fully-unique ones: lifetime
    # 0.2857, recency-weighted (half-life ~50 runs) back above the 0.34 threshold.
    ring = [0.0] * 50 + [1.0] * 20
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _entry(
                "anthropic", "sonnet", attempted=10, completed=10, code_ring=ring
            ),
            "openai/gpt/api": _entry(
                "openai", "gpt", attempted=10, completed=10, code_ring=[1.0] * 8
            ),
        }
    )
    decision = assign_models(_agents(), _cfg(), complexity="LOW", model_profiles=profiles)

    value = decision.routing_decision["code_review"]["value_check"]
    signal = value["signals"]["rev-a"]["uniqueness_rate"]
    assert signal["raw"] == 0.2857  # lifetime: below threshold
    assert signal["rate"] > 0.34  # recency-weighted: recovered
    assert value["deprioritized"] == []
    assert [p.name for p in decision.code_reviewers] == ["rev-a"]

    recovery = value["recovery_check"]
    assert recovery["mechanism"] == MECHANISM_REVIEWER_VALUE_RECOVERY
    assert recovery["fired"] is True
    assert recovery["recovered"] == ["rev-a"]
    assert recovery["checked_detail"]["rev-a"]["below_threshold_lifetime"] is True
    assert "recovered" in recovery["reason"]


def test_recovery_block_is_present_even_when_it_did_not_fire():
    """The return path is never a silent gap (ADR-0006 clause 7)."""
    profiles = _profiles(
        **{
            "anthropic/sonnet/api": _entry(
                "anthropic", "sonnet", attempted=10, completed=9, code_ring=[0.0] * 8
            ),
            "openai/gpt/api": _entry(
                "openai", "gpt", attempted=10, completed=9, code_ring=[1.0] * 8
            ),
        }
    )
    decision = assign_models(_agents(), _cfg(), complexity="LOW", model_profiles=profiles)

    recovery = decision.routing_decision["code_review"]["value_check"]["recovery_check"]
    assert recovery["fired"] is False
    assert recovery["recovered"] == []
    assert sorted(recovery["checked"]) == ["rev-a", "rev-b"]
    assert recovery["reason"] == "no_below_threshold_reviewer_recovered_on_recent_uniqueness"


def test_symmetry_registry_pairs_value_deprioritization_with_recovery():
    """Both directions are registered: exclusion ↔ recovery, neither alone."""
    pair = next(
        p
        for p in ROUTING_SYMMETRY_REGISTRY
        if p.promotion.name == MECHANISM_REVIEWER_VALUE_DEPRIORITIZE
    )
    assert pair.promotion.audit_label == "value_check"
    assert pair.demotion is not None
    assert pair.demotion.name == MECHANISM_REVIEWER_VALUE_RECOVERY
    assert pair.demotion.audit_label == "recovery_check"
    # And a recovery cannot exist without the exclusion side: the recovery
    # mechanism appears in the registry only as this promotion's inverse.
    recovery_pairs = [
        p
        for p in ROUTING_SYMMETRY_REGISTRY
        if p.demotion is not None and p.demotion.name == MECHANISM_REVIEWER_VALUE_RECOVERY
    ]
    assert len(recovery_pairs) == 1
    assert recovery_pairs[0].promotion.name == MECHANISM_REVIEWER_VALUE_DEPRIORITIZE
    assert not any(
        p.promotion.name == MECHANISM_REVIEWER_VALUE_RECOVERY for p in ROUTING_SYMMETRY_REGISTRY
    )


# ── Persistence: separate sections, per reviewer, per complexity band ──────


def test_fold_writes_code_section_and_leaves_plan_section_untouched():
    entry: dict = {}
    fold_reviewer_value(
        entry,
        ReviewerValueSample("rev-a", "high", unique_p1=1, total_p1=4, latency_s=200.0),
        phase=CODE_PHASE,
    )
    assert SECTION not in entry
    section = entry[CODE_SECTION]
    assert section["runs"] == 1
    assert section["by_complexity"]["large"]["avg_uniqueness_rate"] == 0.25
    assert section["by_complexity"]["large"]["avg_latency_per_p1"] == 50.0

    fold_reviewer_value(
        entry,
        ReviewerValueSample("rev-a", "high", unique_p1=4, total_p1=4, latency_s=200.0),
        phase=PLAN_PHASE,
    )
    # The plan fold lands in its own section and does not disturb the code one.
    assert entry[SECTION]["by_complexity"]["large"]["avg_uniqueness_rate"] == 1.0
    assert entry[CODE_SECTION]["by_complexity"]["large"]["avg_uniqueness_rate"] == 0.25


def test_taint_gate_keeps_code_review_samples_out_of_the_aggregate():
    entry: dict = {}
    fold_reviewer_value(
        entry,
        ReviewerValueSample("rev-a", "low", unique_p1=0, total_p1=3, latency_s=90.0),
        phase=CODE_PHASE,
        tainted=True,
    )
    section = entry[CODE_SECTION]
    assert section["runs"] == 0
    assert section["tainted_runs"] == 1
    # "low" normalises to the "small" band, as everywhere else in the profiles.
    assert section["by_complexity"]["small"]["tainted_runs"] == 1


def test_run_outcome_folds_code_reviewer_values_separately_from_plan():
    """Seam test: RunOutcome → apply_run writes the two sections independently."""
    data = apply_run(
        {"models": {}},
        RunOutcome(
            dev_model="dev-cheap",
            complexity="medium",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=0.0,
            plan_reviewer_values=[
                ReviewerValueSample("rev-a", "medium", unique_p1=2, total_p1=2, latency_s=100.0)
            ],
            code_reviewer_values=[
                ReviewerValueSample("rev-a", "medium", unique_p1=0, total_p1=4, latency_s=200.0)
            ],
        ),
    )
    entry = data["models"]["rev-a"]
    assert entry[SECTION]["by_complexity"]["medium"]["avg_uniqueness_rate"] == 1.0
    assert entry[CODE_SECTION]["by_complexity"]["medium"]["avg_uniqueness_rate"] == 0.0
    # And the signal reader resolves each phase to its own section.
    assert (
        get_reviewer_value_signal(data, "rev-a", "medium", 1, phase=CODE_PHASE)["uniqueness_rate"][
            "rate"
        ]
        == 0.0
    )
    assert (
        get_reviewer_value_signal(data, "rev-a", "medium", 1, phase=PLAN_PHASE)["uniqueness_rate"][
            "rate"
        ]
        == 1.0
    )


# ── Uniqueness over code-review findings ───────────────────────────────────


def test_uniqueness_over_code_review_findings_uses_observed_and_evidence():
    """ReviewFinding carries its anchors across observed + evidence, not one blob."""
    from theforge.review import ReviewFinding

    def _f(observed: str, evidence: str, severity: str = "P1") -> ReviewFinding:
        return ReviewFinding(
            severity=severity,
            file="src/theforge/assignment.py",
            line=10,
            observed=observed,
            suggestion=None,
            expected="",
            evidence=evidence,
        )

    shared = _f(
        "The selection helper drops candidates silently.",
        "select_reviewers at assignment.py:3128 ignores the value arguments.",
    )
    peer_shared = _f(
        "Candidates disappear without explanation.",
        "See select_reviewers, which never receives the value flag.",
    )
    solo = _f(
        "The band normaliser mangles an unknown complexity.",
        "normalize_band coerces anything unrecognised to medium.",
    )
    # P2s never participate in the blocking-finding computation.
    noise = _f("Naming could be clearer.", "select_reviewers is terse.", severity="P2")

    result = compute_reviewer_uniqueness(
        [("rev-a", [shared, solo, noise]), ("rev-b", [peer_shared])],
        anchor_text=code_review_anchor_text,
    )
    # rev-a raised 2 blocking findings, one of which rev-b corroborated.
    assert result["rev-a"] == (1, 2)
    # rev-b's single finding was corroborated by rev-a, so nothing unique.
    assert result["rev-b"] == (0, 1)
