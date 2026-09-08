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
    REASON_CAPABILITY_ABSENT,
    REASON_DEV_INCAPABLE,
    REASON_MODEL_UNAVAILABLE,
    AssignmentConfig,
    NoAvailableModelError,
    assign_models,
    routing_stop_message,
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
from theforge.model_availability import (  # noqa: E402
    AVAILABILITY_WARNINGS,
    AvailabilityAnnouncement,
    AvailabilityWarnings,
    StoryAvailability,
    profile_dispatch_key,
)
from theforge.model_capabilities import (  # noqa: E402
    CAPABILITY_TOOL_STRUCTURED,
    OUTCOME_ABSENT,
)

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


def _announce(label: str, answer, identity: str | None = None) -> AvailabilityAnnouncement:
    """One announcement, defaulting the identity to the label."""
    return AvailabilityAnnouncement(identity or f"identity:{label}", label, answer)


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
    answers = [_announce("opus", _unverified()), _announce("sonnet", _available())]
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
    warnings.emit([_announce("opus", _unverified())], lines.append)
    warnings.emit([_announce("opus", _unavailable())], lines.append)
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
    # The exception states the exclusions; the spend belongs to whoever caught
    # it, because only they know what this story has already cost.
    assert "spent" not in message
    assert "$0.00 spent" in routing_stop_message(exc_info.value)
    assert "$1.20 already spent" in routing_stop_message(exc_info.value, 1.2)
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


def _story_availability(config, by_model: dict) -> StoryAvailability:
    """Build the story's answers keyed the way the resolver keys them.

    Tests speak in model names; the router speaks in dispatch identities. This
    translates once, so a test never has to hand-write a dispatch key.
    """
    answers: dict = {}
    profiles = [a.to_model_profile(allowed_tools=()) for a in config.agents]
    profiles += [config.preflight_profile, config.dev_profile, *config.review_pool]
    profiles += list(config.plan_agent_review.profiles or ())
    if config.preflight_fallback_profile is not None:
        profiles.append(config.preflight_fallback_profile)
    for profile in profiles:
        key = profile_dispatch_key(profile, config)
        if key is not None and profile.model in by_model:
            answers[key] = by_model[profile.model]
    return StoryAvailability(answers)


def _patch_availability(config, by_model: dict):
    """Patch the coordinator's story-boundary resolution with fixed answers."""
    return patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=_story_availability(config, by_model),
    )


def _proceed_state() -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5
    return state


def test_preflight_seam_excludes_the_unavailable_model_from_the_installed_config(tmp_path):
    config = _adaptive_config(tmp_path)
    with _patch_availability(
        config, {"opus": _unavailable(), "sonnet": _available(), "haiku": _available()}
    ):
        applied = _apply_preflight_config(config, _proceed_state(), task_slug="s1")
    seated = {applied.dev_profile.model, *[p.model for p in applied.review_pool]}
    assert "opus" not in seated


def test_availability_is_resolved_fresh_for_each_story(tmp_path):
    """An answer that changes between stories changes routing without a restart."""
    config = _adaptive_config(tmp_path)
    answers = [
        _story_availability(
            config, {"opus": _available(), "sonnet": _available(), "haiku": _available()}
        ),
        _story_availability(
            config, {"opus": _unavailable(), "sonnet": _available(), "haiku": _available()}
        ),
    ]
    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
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
    with _patch_availability(
        config, {name: _unavailable() for name in ("haiku", "sonnet", "opus")}
    ):
        with pytest.raises(NoAvailableModelError):
            _apply_preflight_config(config, _proceed_state(), task_slug="s1")

    forge_dir = tmp_path / ".forge"
    assert not (forge_dir / "model_profiles.yaml").exists()
    assert not (forge_dir / "model_capabilities.yaml").exists()
    assert not (forge_dir / "assignment_history.yaml").exists()


def test_a_jointly_emptied_pool_names_every_cause_not_just_availability():
    """A pool emptied by two rules must not read as one rule's fault.

    The account cannot invoke one candidate and the durable record rules the
    other out. A stop naming only the availability exclusion would send the
    operator to fix their credentials when half the answer is the capability
    record.
    """
    capability_records = {
        "version": 1,
        "identities": {
            "anthropic/sonnet/api": {
                "provider": "anthropic",
                "model": "sonnet",
                "transport": "api",
                "capabilities": {
                    CAPABILITY_TOOL_STRUCTURED: {
                        "outcome": OUTCOME_ABSENT,
                        "established_at": "2026-09-01T00:00:00Z",
                        "subject_signature": "",
                        "detail": "returned prose",
                        "probe_role": "agent-code-review",
                    }
                },
            }
        },
    }
    agents = [a for a in _agents() if a.name in {"sonnet", "opus"}]
    with pytest.raises(NoAvailableModelError) as exc_info:
        assign_models(
            agents,
            _cfg(),
            "medium",
            complexity_score=5,
            capability_records=capability_records,
            model_availability={"opus": _unavailable()},
        )
    excluded = exc_info.value.excluded
    assert excluded["opus"]["reason"] == REASON_MODEL_UNAVAILABLE
    assert excluded["sonnet"]["reason"] == REASON_CAPABILITY_ABSENT
    message = str(exc_info.value)
    assert "not available to this account" in message
    assert "demonstrated absent" in message


def test_a_pool_emptied_by_availability_and_a_dev_declaration_names_both():
    agents = [
        replace(a, dev_capable=False) if a.name == "sonnet" else a
        for a in _agents()
        if a.name in {"sonnet", "opus"}
    ]
    with pytest.raises(NoAvailableModelError) as exc_info:
        assign_models(
            agents,
            _cfg(),
            "medium",
            complexity_score=5,
            model_availability={"opus": _unavailable()},
        )
    if exc_info.value.role == "dev":
        assert exc_info.value.excluded["sonnet"]["reason"] == REASON_DEV_INCAPABLE
        assert "declared dev_capable=false" in str(exc_info.value)


# ── Explicit pools are filtered as whole pools, not just their head ────


def _reviewer(name: str, model: str) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider="anthropic",
        model=model,
        budget_usd=2.0,
        timeout_seconds=600,
        allowed_tools=(),
    )


def _pinned_pool_config(tmp_path):
    """A config whose reviewer pool the operator pinned to two models."""
    return replace(
        _adaptive_config(tmp_path),
        review_pool=[_reviewer("r1", "sonnet"), _reviewer("r2", "opus")],
        review_pool_is_default=False,
    )


def test_an_unavailable_non_leading_pinned_reviewer_is_not_dispatched(tmp_path):
    """The pool is spliced back whole, so it must be filtered whole.

    Filtering only the pool's head — the entry that reaches assign_models as the
    role's pin — leaves every later member to be dispatched unchecked.
    """
    config = _pinned_pool_config(tmp_path)
    with _patch_availability(config, {"sonnet": _available(), "opus": _unavailable()}):
        state = _proceed_state()
        applied = _apply_preflight_config(config, state, task_slug="s1")

    assert [p.model for p in applied.review_pool] == ["sonnet"]
    pool = state.routing_decision["code_review"]["candidate_pool"]
    seated = {entry["name"] for entry in pool if entry.get("included")}
    assert "opus" not in seated, "an unavailable pinned reviewer must not read as included"


def test_a_pinned_pool_with_nothing_available_stops_the_run(tmp_path):
    config = _pinned_pool_config(tmp_path)
    with _patch_availability(config, {"sonnet": _unavailable(), "opus": _unavailable()}):
        with pytest.raises(NoAvailableModelError) as exc_info:
            _apply_preflight_config(config, _proceed_state(), task_slug="s1")
    assert exc_info.value.role in {"code_review", "preflight", "dev"}


# ── Static routing (adaptive disabled) honours availability too ────────


def _static_config(tmp_path):
    return replace(
        _make_config(tmp_path),
        agents=[],
        assignment=_cfg(enabled=False),
        models=None,
    )


def test_static_routing_refuses_an_unavailable_fixed_profile(tmp_path):
    """With no pool to narrow, the only thing availability can do is refuse."""
    config = _static_config(tmp_path)
    by_model = {p.model: _unavailable() for p in (config.dev_profile, config.preflight_profile)}
    with _patch_availability(config, by_model):
        with pytest.raises(NoAvailableModelError):
            _apply_preflight_config(config, _proceed_state(), task_slug="s1")


def test_static_routing_warns_once_for_an_unverified_fixed_profile(tmp_path):
    config = _static_config(tmp_path)
    lines: list[str] = []
    AVAILABILITY_WARNINGS.reset("test-static")
    with _patch_availability(config, {config.dev_profile.model: _unverified()}):
        _apply_preflight_config(config, _proceed_state(), log=lines.append, task_slug="s1")
        _apply_preflight_config(config, _proceed_state(), log=lines.append, task_slug="s2")
    warnings = [line for line in lines if "availability unconfirmed" in line]
    assert len(warnings) == 1, f"expected one warning per model per run, got {warnings}"


def test_static_routing_leaves_an_available_fixed_profile_alone(tmp_path):
    config = _static_config(tmp_path)
    with _patch_availability(config, {config.dev_profile.model: _available()}):
        applied = _apply_preflight_config(config, _proceed_state(), task_slug="s1")
    assert applied.dev_profile.model == config.dev_profile.model


# ── The warning scope is the run, not the process ──────────────────────


def test_a_later_run_in_the_same_process_warns_again():
    """Two sprints in one process are two runs, and each gets its warning.

    Without run scoping the daemon's second sprint inherits the first one's
    warned set and announces nothing — "once per process", not "once per run".
    """
    warnings = AvailabilityWarnings()
    answers = [_announce("opus", _unverified())]
    first: list[str] = []
    second: list[str] = []

    warnings.emit(answers, first.append, run_key="sprint:one")
    warnings.emit(answers, first.append, run_key="sprint:one")
    warnings.emit(answers, second.append, run_key="sprint:two")

    assert len(first) == 1, "one warning per model within a run"
    assert len(second) == 1, "a new run announces the model again"


def test_concurrent_stories_in_one_run_warn_exactly_once():
    """Parallel workers share the tracker, so the check-then-mark must be atomic."""
    import threading

    warnings = AvailabilityWarnings()
    answers = [_announce("opus", _unverified())]
    emitted: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker() -> None:
        start.wait()
        for entry in warnings.pending(answers, "sprint:parallel"):
            with lock:
                emitted.append(entry.label)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert emitted == ["opus"]


# ── A dropped pinned reviewer stays visible in the routing decision ────


def test_a_dropped_pinned_reviewer_is_recorded_as_model_unavailable(tmp_path):
    """Filtering the pool is half the job; explaining it is the other half.

    A pinned reviewer the account cannot invoke must not simply vanish from the
    decision, and must not be reported under some other rule — an operator
    reading the record has to see the account answer that removed their own
    configured reviewer.
    """
    config = _pinned_pool_config(tmp_path)
    with _patch_availability(config, {"sonnet": _available(), "opus": _unavailable()}):
        state = _proceed_state()
        _apply_preflight_config(config, state, task_slug="s1")

    pool = state.routing_decision["code_review"]["candidate_pool"]
    dropped = next(e for e in pool if e["name"] in {"r2", "opus"} and not e.get("included"))
    assert dropped["reason"] == REASON_MODEL_UNAVAILABLE, (
        "the pinned reviewer's exclusion must name the account answer, "
        "not the generic override lock"
    )
    assert dropped["detail"]["auth_mode"] == "ChatGPT-account auth"
    assert dropped["detail"]["checked_at"] == CHECKED_AT.isoformat()


# ── One identity, two configuration labels, one warning ───────────────


def test_one_identity_named_twice_in_config_warns_once(tmp_path):
    """A model configured as a pool agent AND as a phase profile is one model.

    Keying the warning on the label rather than the dispatch identity announced
    the same unverified model twice — once under its agent name and once under
    its model string (#2950 review).
    """
    shared = AgentDef(
        name="the-pool-name",
        provider="anthropic",
        model="sonnet",
        budget_usd=5.0,
        timeout_seconds=900,
        tier="mid",
    )
    config = replace(
        _adaptive_config(tmp_path),
        agents=[shared],
        dev_profile=shared.to_model_profile(allowed_tools=()),
        preflight_profile=shared.to_model_profile(allowed_tools=()),
    )
    lines: list[str] = []
    AVAILABILITY_WARNINGS.reset("test-one-identity")
    with _patch_availability(config, {"sonnet": _unverified()}):
        _apply_preflight_config(config, _proceed_state(), log=lines.append, task_slug="s1")

    warnings = [line for line in lines if "availability unconfirmed" in line]
    assert len(warnings) == 1, f"one identity is one warning, got {warnings}"


# ── Static routing filters mixed pools rather than dispatching them ───


def _static_pool_config(tmp_path):
    """Adaptive routing off, with a two-model pinned reviewer pool."""
    return replace(
        _static_config(tmp_path),
        review_pool=[_reviewer("r1", "sonnet"), _reviewer("r2", "opus")],
        review_pool_is_default=False,
    )


def test_static_routing_drops_the_unavailable_member_of_a_mixed_pool(tmp_path):
    """No adaptive pool does not mean no pool: a reviewer pool is still a pool."""
    config = _static_pool_config(tmp_path)
    with _patch_availability(config, {"sonnet": _available(), "opus": _unavailable()}):
        applied = _apply_preflight_config(config, _proceed_state(), task_slug="s1")

    assert [p.model for p in applied.review_pool] == ["sonnet"], (
        "the unavailable member must not remain dispatchable under static routing"
    )


def test_static_routing_keeps_a_fully_available_pool_intact(tmp_path):
    config = _static_pool_config(tmp_path)
    with _patch_availability(config, {"sonnet": _available(), "opus": _available()}):
        applied = _apply_preflight_config(config, _proceed_state(), task_slug="s1")
    assert [p.model for p in applied.review_pool] == ["sonnet", "opus"]


def test_static_routing_refuses_only_when_the_whole_pool_is_gone(tmp_path):
    config = _static_pool_config(tmp_path)
    with _patch_availability(config, {"sonnet": _unavailable(), "opus": _unavailable()}):
        with pytest.raises(NoAvailableModelError):
            _apply_preflight_config(config, _proceed_state(), task_slug="s1")


# ── Same model string, two identities, both named in the stop ─────────


def test_a_fixed_profile_stop_names_both_identities_sharing_a_model_string(tmp_path):
    """Two endpoints are two candidates, and a stop must name both.

    Keying the refusal payload on the model name collapsed them into one entry,
    so the operator saw one exclusion where there were two — and only one of the
    two reasons (#2950 review).
    """
    config = replace(
        _static_config(tmp_path),
        review_pool=[
            replace(_reviewer("r1", "gpt-5"), provider="openai", base_url="http://127.0.0.1:1/v1"),
            replace(_reviewer("r2", "gpt-5"), provider="openai", base_url="http://127.0.0.1:2/v1"),
        ],
        review_pool_is_default=False,
    )
    answers = {}
    for profile in config.review_pool:
        key = profile_dispatch_key(profile, config)
        reason = f"not in account catalog for {profile.base_url}"
        answers[key] = ModelAvailability(
            MODEL_AVAILABILITY_UNAVAILABLE,
            "ChatGPT-account auth",
            CHECKED_AT,
            reason,
            AVAILABILITY_FRESHNESS_CURRENT,
        )
    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=StoryAvailability(answers),
    ):
        with pytest.raises(NoAvailableModelError) as exc_info:
            _apply_preflight_config(config, _proceed_state(), task_slug="s1")

    excluded = exc_info.value.excluded
    assert len(excluded) == 2, f"two identities, two exclusions: {excluded}"
    message = str(exc_info.value)
    assert "http://127.0.0.1:1/v1" in message and "http://127.0.0.1:2/v1" in message
    assert message.count("gpt-5 excluded") == 2, (
        "both candidates are named, under one model string"
    )


def test_two_identities_sharing_a_model_string_announce_separately(tmp_path):
    """The mirror of the dedup: one name, two identities, two announcements.

    Deduplicating announcements on the model string silenced one of two
    genuinely different endpoints. Identity is the key; the label is only how it
    reads.
    """
    from theforge.model_availability import announcements

    config = _static_config(tmp_path)
    endpoints = [
        replace(_reviewer("r1", "gpt-5"), provider="openai", base_url="http://127.0.0.1:1/v1"),
        replace(_reviewer("r2", "gpt-5"), provider="openai", base_url="http://127.0.0.1:2/v1"),
    ]
    answers = {profile_dispatch_key(p, config): _unverified() for p in endpoints}
    entries = announcements(config, StoryAvailability(answers), profiles=endpoints)

    assert len(entries) == 2, "two endpoints are two answers, even under one model name"
    assert {entry.label for entry in entries} == {"gpt-5"}
    assert len({entry.identity for entry in entries}) == 2


# ── A pin outside the agents pool is checked on its own identity ──────


def test_a_dev_pin_absent_from_the_pool_is_refused_when_unavailable(tmp_path):
    """assign_models can only recognise a pin it can find in the pool.

    An operator pinning `dev` to a model that is not also a pool agent got no
    availability check at all: the pin reached dispatch while the routing
    decision showed only the pool as locked out (#2950 review).
    """
    pinned = ModelProfile(
        name="pinned-dev",
        provider="openai",
        model="gpt-5",
        budget_usd=6.0,
        timeout_seconds=900,
        allowed_tools=(),
    )
    config = replace(_adaptive_config(tmp_path), dev_profile=pinned)
    assert all(a.model != pinned.model for a in config.agents), "the pin is outside the pool"

    answers = {
        profile_dispatch_key(pinned, config): _unavailable(),
        **{
            profile_dispatch_key(a.to_model_profile(allowed_tools=()), config): _available()
            for a in config.agents
        },
    }
    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=StoryAvailability(answers),
    ):
        with pytest.raises(NoAvailableModelError) as exc_info:
            _apply_preflight_config(config, _proceed_state(), task_slug="s1")

    assert exc_info.value.role == "dev"
    assert "gpt-5" in str(exc_info.value)


def test_an_available_pin_outside_the_pool_still_routes(tmp_path):
    """The check is availability, not membership: an available pin is honoured."""
    pinned = ModelProfile(
        name="pinned-dev",
        provider="openai",
        model="gpt-5",
        budget_usd=6.0,
        timeout_seconds=900,
        allowed_tools=(),
    )
    config = replace(_adaptive_config(tmp_path), dev_profile=pinned)
    answers = {
        profile_dispatch_key(pinned, config): _available(),
        **{
            profile_dispatch_key(a.to_model_profile(allowed_tools=()), config): _available()
            for a in config.agents
        },
    }
    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=StoryAvailability(answers),
    ):
        applied = _apply_preflight_config(config, _proceed_state(), task_slug="s1")
    assert applied.dev_profile.model == "gpt-5"


# ── Static exclusions are recorded, not just applied ──────────────────


def test_static_filtering_is_recorded_in_the_routing_decision(tmp_path):
    """Filtering a static pool without recording it leaves nothing to diagnose."""
    config = replace(
        _static_config(tmp_path),
        review_pool=[_reviewer("r1", "sonnet"), _reviewer("r2", "opus")],
        review_pool_is_default=False,
    )
    state = _proceed_state()
    with _patch_availability(config, {"sonnet": _available(), "opus": _unavailable()}):
        _apply_preflight_config(config, state, task_slug="s1")

    pool = state.routing_decision["code_review"]["candidate_pool"]
    dropped = next(e for e in pool if not e["included"])
    assert dropped["reason"] == REASON_MODEL_UNAVAILABLE
    assert dropped["detail"]["model"] == "opus"
    assert dropped["detail"]["auth_mode"] == "ChatGPT-account auth"
    assert dropped["detail"]["checked_at"] == CHECKED_AT.isoformat()
    assert [e["name"] for e in pool if e["included"]] == ["r1"]


def test_static_filtering_does_not_narrate_the_preflight_reseat(tmp_path):
    """One runtime event, one narration: the dispatch check owns preflight's."""
    config = _static_config(tmp_path)
    lines: list[str] = []
    with _patch_availability(config, {config.preflight_profile.model: _available()}):
        _apply_preflight_config(config, _proceed_state(), log=lines.append, task_slug="s1")
    assert not [line for line in lines if "preflight: dropped unavailable" in line]


# ── The post-plan checkpoint will not preserve an unreachable incumbent ─


def _post_plan(decision, agents, *, availability, capability_records=None):
    from theforge.assignment import apply_post_plan_checkpoint

    return apply_post_plan_checkpoint(
        decision,
        agents,
        _cfg(),
        "medium",
        plan_review_decision="APPROVE",
        plan_review_cycles=1,
        p1_count=0,
        p2_count=0,
        model_availability=availability,
        capability_records=capability_records,
    )


def test_the_checkpoint_reroutes_when_the_seated_dev_became_unavailable():
    """Preserving the incumbent is right for a demotion, wrong for a dead model.

    Every bypass path in the checkpoint returns the seated dev unchanged. When
    the account lost access to that model since preflight, that hands the dev
    phase a model it cannot invoke (#2950 review).
    """
    agents = _agents()
    decision = assign_models(agents, _cfg(), "medium", complexity_score=5)
    seated = decision.dev.name

    updated = _post_plan(
        decision,
        agents,
        availability={
            name: (_unavailable() if name == seated else _available())
            for name in (a.name for a in agents)
        },
    )
    assert updated.dev.name != seated, "the unreachable incumbent must not survive"
    block = updated.routing_decision["dev"]["post_plan_checkpoint"]
    assert block["rationale"] == "incumbent_unavailable"
    assert block["decision"] == "reroute"


def test_the_checkpoint_refuses_when_nothing_is_left_to_reroute_onto():
    agents = _agents()
    decision = assign_models(agents, _cfg(), "medium", complexity_score=5)

    with pytest.raises(NoAvailableModelError) as exc_info:
        _post_plan(
            decision,
            agents,
            availability={a.name: _unavailable() for a in agents},
        )
    assert exc_info.value.role == "dev"
    assert set(exc_info.value.excluded) == {a.name for a in agents}


def test_the_checkpoint_leaves_an_available_incumbent_alone():
    """Parity: the ordinary demotion question is unchanged."""
    agents = _agents()
    decision = assign_models(agents, _cfg(), "medium", complexity_score=5)
    baseline = _post_plan(decision, agents, availability=None)
    with_answers = _post_plan(
        decision, agents, availability={a.name: _available() for a in agents}
    )
    assert with_answers.dev.model == baseline.dev.model
