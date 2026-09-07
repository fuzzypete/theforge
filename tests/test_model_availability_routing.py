"""Availability as a routing input (#2950).

Covers the four acceptance criteria of the slice: an unavailable model appears
in no phase's candidate pool and is recorded with a canonical reason; an
unverified one routes exactly as it does today and is warned about once; a
phase with nothing left stops the run before any spend as a routing outcome;
and the answer is read at the moment of selection, so it can change mid-sprint.

Seam coverage (CONVENTIONS rule 8) runs through
``_apply_preflight_config`` — the coordinator boundary where availability is
resolved and handed to ``assign_models`` — not just through the pure function.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import _make_config  # noqa: E402

from theforge.assignment import (  # noqa: E402
    REASON_MODEL_UNAVAILABLE,
    AssignmentConfig,
    NoAvailableModelError,
    assign_models,
)
from theforge.config import AgentDef, ModelProfile  # noqa: E402
from theforge.config.model_identity import (  # noqa: E402
    AVAILABILITY_FRESHNESS_CURRENT,
    MODEL_AVAILABILITY_AVAILABLE,
    MODEL_AVAILABILITY_UNAVAILABLE,
    MODEL_AVAILABILITY_UNVERIFIED,
    ModelAvailability,
)
from theforge.coordinator.preflight import _apply_preflight_config  # noqa: E402
from theforge.coordinator.state import CoordinatorState  # noqa: E402
from theforge.model_availability import AvailabilityWarnings  # noqa: E402

CHECKED_AT = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

ROLES = ("preflight", "planner", "dev", "plan_review", "code_review")


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    """Auth readiness is a separate axis; keep every test agent authed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


def _unavailable(reason: str = "not in account catalog") -> ModelAvailability:
    return ModelAvailability(
        MODEL_AVAILABILITY_UNAVAILABLE,
        "ChatGPT-account auth",
        CHECKED_AT,
        reason,
        AVAILABILITY_FRESHNESS_CURRENT,
    )


def _unverified(reason: str = "provider publishes no account catalog") -> ModelAvailability:
    return ModelAvailability(
        MODEL_AVAILABILITY_UNVERIFIED,
        "API-key auth",
        CHECKED_AT,
        reason,
        AVAILABILITY_FRESHNESS_CURRENT,
    )


def _available() -> ModelAvailability:
    return ModelAvailability(
        MODEL_AVAILABILITY_AVAILABLE,
        "API-key auth",
        CHECKED_AT,
        None,
        AVAILABILITY_FRESHNESS_CURRENT,
    )


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=100.0,
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
        ),
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


def _entry(decision, role: str, name: str) -> dict:
    pool = decision.routing_decision[role]["candidate_pool"]
    return next(e for e in pool if e["name"] == name)


# ── AC 1: unavailable appears in no phase's candidate pool ─────────────


def test_unavailable_model_is_excluded_from_every_phase_pool():
    decision = assign_models(
        _agents(),
        _cfg(),
        "medium",
        complexity_score=5,
        model_availability={"opus": _unavailable(), "sonnet": _available()},
    )
    for role in ROLES:
        entry = _entry(decision, role, "opus")
        assert entry["included"] is False, f"{role} still admitted an unavailable model"
        assert entry["reason"] == REASON_MODEL_UNAVAILABLE


def test_unavailable_model_is_never_seated_in_any_role():
    decision = assign_models(
        _agents(),
        _cfg(),
        "large",
        complexity_score=8,
        model_availability={"opus": _unavailable()},
    )
    seated = {
        decision.preflight.model,
        decision.planner.model,
        decision.dev.model,
        *[p.model for p in decision.plan_reviewers],
        *[p.model for p in decision.code_reviewers],
    }
    assert "opus" not in seated


def test_exclusion_detail_names_auth_mode_freshness_and_time():
    """The recorded detail must let an operator judge the answer, not just see it."""
    decision = assign_models(
        _agents(),
        _cfg(),
        "medium",
        complexity_score=5,
        model_availability={"opus": _unavailable()},
    )
    detail = _entry(decision, "dev", "opus")["detail"]
    assert detail == {
        "state": MODEL_AVAILABILITY_UNAVAILABLE,
        "auth_mode": "ChatGPT-account auth",
        "checked_at": CHECKED_AT.isoformat(),
        "freshness": AVAILABILITY_FRESHNESS_CURRENT,
        "reason": "not in account catalog",
    }


def test_exclusions_are_recorded_on_the_routing_decision_roles():
    """Same place, same shape as the other canonical exclusions."""
    decision = assign_models(
        _agents(),
        _cfg(),
        "medium",
        complexity_score=5,
        model_availability={"opus": _unavailable()},
    )
    for role in ROLES:
        pool = decision.routing_decision[role]["candidate_pool"]
        assert {e["name"] for e in pool} == {"haiku", "sonnet", "opus"}
        assert all("included" in e and "reason" in e for e in pool)


def test_two_agents_on_one_model_get_independent_answers():
    """Availability is scoped to a dispatch identity, and pools key on the name.

    Two pool entries naming the same model under different credentials are
    separate agents; one being unavailable must not exclude the other.
    """
    agents = [
        AgentDef(
            name="gpt-account",
            provider="openai",
            model="gpt-5",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="gpt-apikey",
            provider="openai",
            model="gpt-5",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
    ]
    decision = assign_models(
        agents,
        _cfg(),
        "medium",
        complexity_score=5,
        model_availability={"gpt-account": _unavailable(), "gpt-apikey": _available()},
    )
    assert _entry(decision, "dev", "gpt-account")["reason"] == REASON_MODEL_UNAVAILABLE
    assert _entry(decision, "dev", "gpt-apikey")["included"] is True
    assert decision.dev.name == "gpt-apikey"


# ── AC 2: unverified stays fully eligible, warned once ─────────────────


def test_unverified_routes_identically_to_no_answer_at_all():
    baseline = assign_models(_agents(), _cfg(), "medium", complexity_score=5)
    unverified = assign_models(
        _agents(),
        _cfg(),
        "medium",
        complexity_score=5,
        model_availability={name: _unverified() for name in ("haiku", "sonnet", "opus")},
    )
    assert unverified.dev.model == baseline.dev.model
    assert unverified.preflight.model == baseline.preflight.model
    assert unverified.planner.model == baseline.planner.model
    assert [p.model for p in unverified.code_reviewers] == [
        p.model for p in baseline.code_reviewers
    ]
    assert [p.model for p in unverified.plan_reviewers] == [
        p.model for p in baseline.plan_reviewers
    ]
    for role in ROLES:
        assert (
            unverified.routing_decision[role]["candidate_pool"]
            == (baseline.routing_decision[role]["candidate_pool"])
        )


def test_unverified_model_warns_exactly_once_across_stories():
    warnings = AvailabilityWarnings()
    answers = {"opus": _unverified(), "sonnet": _available()}
    lines: list[str] = []

    first = warnings.emit(answers, lines.append)
    second = warnings.emit(answers, lines.append)
    third = warnings.emit(answers, lines.append)

    assert first == ["opus"]
    assert second == [] and third == []
    assert len(lines) == 1
    assert "opus" in lines[0] and "unconfirmed" in lines[0]


def test_a_changed_answer_is_announced_again():
    """Suppression is per answer, not per model: a model that becomes
    unavailable mid-sprint must not be silenced by its earlier warning."""
    warnings = AvailabilityWarnings()
    lines: list[str] = []
    warnings.emit({"opus": _unverified()}, lines.append)
    warnings.emit({"opus": _unavailable()}, lines.append)
    assert len(lines) == 2
    assert "not available to this account" in lines[1]


# ── AC 3: a phase with nothing left stops before any spend ─────────────


def test_routing_refuses_when_every_candidate_is_unavailable():
    with pytest.raises(NoAvailableModelError) as exc_info:
        assign_models(
            _agents(),
            _cfg(),
            "medium",
            complexity_score=5,
            model_availability={name: _unavailable() for name in ("haiku", "sonnet", "opus")},
        )
    message = str(exc_info.value)
    for name in ("haiku", "sonnet", "opus"):
        assert name in message, f"the stop must name {name}"
    assert "not available to this account under ChatGPT-account auth" in message
    assert "$0.00 spent" in message
    assert exc_info.value.exclusion_reason == REASON_MODEL_UNAVAILABLE
    assert set(exc_info.value.excluded) == {"haiku", "sonnet", "opus"}


def test_refusal_names_the_phase():
    with pytest.raises(NoAvailableModelError) as exc_info:
        assign_models(
            _agents(),
            _cfg(),
            "medium",
            complexity_score=5,
            model_availability={name: _unavailable() for name in ("haiku", "sonnet", "opus")},
        )
    assert exc_info.value.role in ROLES
    assert f"phase {exc_info.value.role}" in str(exc_info.value)


def test_an_unavailable_explicit_pin_refuses_rather_than_dispatching():
    """An operator pin the account cannot invoke has no dispatch to honor."""
    pin = ModelProfile(
        name="opus",
        provider="anthropic",
        model="opus",
        budget_usd=8.0,
        timeout_seconds=1200,
        allowed_tools=(),
    )
    with pytest.raises(NoAvailableModelError) as exc_info:
        assign_models(
            _agents(),
            _cfg(),
            "medium",
            complexity_score=5,
            explicit_profiles={"dev": pin},
            model_availability={"opus": _unavailable()},
        )
    assert "opus" in exc_info.value.excluded


def test_unverified_never_empties_a_pool():
    """A provider that publishes no catalog must never stop a run."""
    decision = assign_models(
        _agents(),
        _cfg(),
        "medium",
        complexity_score=5,
        model_availability={name: _unverified() for name in ("haiku", "sonnet", "opus")},
    )
    assert decision.dev.model


# ── AC 4 + seam: resolved at the moment of selection ───────────────────


def _adaptive_config(tmp_path):
    return replace(
        _make_config(tmp_path),
        agents=_agents(),
        assignment=_cfg(max_cost_per_story_usd=100.0),
        models=None,
        # No pinned roles: every phase draws from the adaptive pool, which is
        # what makes the availability filter observable at this seam.
        review_pool_is_default=True,
        plan_model_is_default=True,
    )


def _proceed_state() -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5
    return state


def test_preflight_seam_excludes_the_unavailable_model_from_the_installed_config(tmp_path):
    config = _adaptive_config(tmp_path)
    with patch(
        "theforge.model_availability.resolve_agent_availability",
        return_value={"opus": _unavailable(), "sonnet": _available(), "haiku": _available()},
    ):
        applied = _apply_preflight_config(config, _proceed_state(), task_slug="s1")
    seated = {applied.dev_profile.model, *[p.model for p in applied.review_pool]}
    assert "opus" not in seated


def test_availability_is_resolved_fresh_for_each_story(tmp_path):
    """An answer that changes between stories changes routing without a restart."""
    config = _adaptive_config(tmp_path)
    answers = [
        {"opus": _available(), "sonnet": _available(), "haiku": _available()},
        {"opus": _unavailable(), "sonnet": _available(), "haiku": _available()},
    ]
    with patch(
        "theforge.model_availability.resolve_agent_availability",
        side_effect=answers,
    ) as resolver:
        first_state, second_state = _proceed_state(), _proceed_state()
        _apply_preflight_config(config, first_state, task_slug="story-1")
        _apply_preflight_config(config, second_state, task_slug="story-2")

    assert resolver.call_count == 2, "availability must be read at each selection boundary"
    first_pool = first_state.routing_decision["dev"]["candidate_pool"]
    second_pool = second_state.routing_decision["dev"]["candidate_pool"]
    first_opus = next(e for e in first_pool if e["name"] == "opus")
    second_opus = next(e for e in second_pool if e["name"] == "opus")
    assert first_opus["reason"] != REASON_MODEL_UNAVAILABLE
    assert second_opus["reason"] == REASON_MODEL_UNAVAILABLE


def test_preflight_seam_refusal_writes_no_capability_or_profile_state(tmp_path):
    """The stop is a routing outcome: nothing may be recorded against a model."""
    config = _adaptive_config(tmp_path)
    with patch(
        "theforge.model_availability.resolve_agent_availability",
        return_value={name: _unavailable() for name in ("haiku", "sonnet", "opus")},
    ):
        with pytest.raises(NoAvailableModelError):
            _apply_preflight_config(config, _proceed_state(), task_slug="s1")

    forge_dir = tmp_path / ".forge"
    assert not (forge_dir / "model_profiles.yaml").exists()
    assert not (forge_dir / "model_capabilities.yaml").exists()
    assert not (forge_dir / "assignment_history.yaml").exists()
