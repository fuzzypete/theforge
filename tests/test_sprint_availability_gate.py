"""Pre-dispatch availability gate (#2950).

The gate answers, before a sprint spends anything, whether each required phase
has any model the account can invoke. These tests pin the two properties that
make it safe to run ahead of every dispatch: it stops on positive catalog
evidence with a message naming every excluded model and the spend, and an
unverified answer never stops anything.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import _make_config  # noqa: E402

from theforge.config import AgentDef, AssignmentConfig  # noqa: E402
from theforge.config.model_identity import (  # noqa: E402
    AVAILABILITY_FRESHNESS_CURRENT,
    MODEL_AVAILABILITY_AVAILABLE,
    MODEL_AVAILABILITY_UNAVAILABLE,
    MODEL_AVAILABILITY_UNVERIFIED,
    ModelAvailability,
)
from theforge.sprint.availability_gate import (  # noqa: E402
    SprintNoAvailableModel,
    check_sprint_availability,
    enforce_sprint_availability,
    preflight_dispatch_profiles,
)

CHECKED_AT = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _answer(state: str, reason: str | None = None) -> ModelAvailability:
    return ModelAvailability(
        state,
        "ChatGPT-account auth",
        CHECKED_AT,
        reason,
        AVAILABILITY_FRESHNESS_CURRENT,
    )


def _agents() -> list[AgentDef]:
    return [
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


def _config(tmp_path):
    return replace(
        _make_config(tmp_path),
        agents=_agents(),
        assignment=AssignmentConfig(enabled=True, max_cost_per_story_usd=50.0),
        models=None,
        review_pool_is_default=True,
        plan_model_is_default=True,
    )


def _resolver(state: str, reason: str | None = None):
    def resolve(targets, _secrets=None):
        return {target.key: _answer(state, reason) for target in targets}

    return resolve


def test_gate_stops_when_every_candidate_is_unavailable(tmp_path):
    stops = check_sprint_availability(
        _config(tmp_path), resolve=_resolver(MODEL_AVAILABILITY_UNAVAILABLE, "not in catalog")
    )
    assert stops, "a phase with no invocable model must stop the sprint"
    assert {stop.phase for stop in stops} >= {"dev"}
    dev_stop = next(stop for stop in stops if stop.phase == "dev")
    assert dev_stop.models == ["opus", "sonnet"]


def test_gate_message_names_every_model_and_the_spend(tmp_path):
    logged: list[str] = []
    with pytest.raises(SprintNoAvailableModel) as exc_info:
        enforce_sprint_availability(
            _config(tmp_path),
            log=logged.append,
            resolve=_resolver(MODEL_AVAILABILITY_UNAVAILABLE, "not in account catalog"),
        )
    message = str(exc_info.value)
    assert "no model available for phase" in message
    assert "sonnet" in message and "opus" in message
    assert "not available to this account under ChatGPT-account auth" in message
    assert "not in account catalog" in message
    assert "$0.00 spent" in message
    assert "none was marked failed" in message
    assert logged, "the operator must see the stop in the run log"


def test_gate_is_silent_when_every_answer_is_unverified(tmp_path):
    """A provider that publishes no catalog must never stop a sprint."""
    assert (
        check_sprint_availability(
            _config(tmp_path),
            resolve=_resolver(MODEL_AVAILABILITY_UNVERIFIED, "provider publishes no catalog"),
        )
        == []
    )
    enforce_sprint_availability(
        _config(tmp_path),
        resolve=_resolver(MODEL_AVAILABILITY_UNVERIFIED, "provider publishes no catalog"),
    )


def test_gate_is_silent_when_models_are_available(tmp_path):
    enforce_sprint_availability(_config(tmp_path), resolve=_resolver(MODEL_AVAILABILITY_AVAILABLE))


def test_one_unavailable_candidate_does_not_stop_a_phase_with_alternatives(tmp_path):
    """The gate is a whole-pool question; narrowing is the router's job."""

    def resolve(targets, _secrets=None):
        return {
            target.key: _answer(
                MODEL_AVAILABILITY_UNAVAILABLE
                if target.model == "opus"
                else MODEL_AVAILABILITY_AVAILABLE
            )
            for target in targets
        }

    assert check_sprint_availability(_config(tmp_path), resolve=resolve) == []


def test_a_resolver_failure_never_stops_a_sprint(tmp_path):
    def explode(_targets, _secrets=None):
        raise RuntimeError("catalog unreachable")

    assert check_sprint_availability(_config(tmp_path), resolve=explode) == []


def test_same_model_under_two_endpoints_counts_as_two_candidates(tmp_path):
    """Identity, not model string, is what a candidate is.

    A phase holding two profiles that share a model name but dispatch to
    different endpoints has two candidates. Counting them as one would let a
    phase with both unavailable look like it still had a survivor, and the
    sprint would launch into a dev phase that cannot run.
    """
    config = replace(
        _config(tmp_path),
        agents=[
            AgentDef(
                name="local-a",
                provider="openai",
                model="gpt-5",
                budget_usd=5.0,
                timeout_seconds=900,
                tier="mid",
                base_url="http://127.0.0.1:8001/v1",
            ),
            AgentDef(
                name="local-b",
                provider="openai",
                model="gpt-5",
                budget_usd=5.0,
                timeout_seconds=900,
                tier="mid",
                base_url="http://127.0.0.1:8002/v1",
            ),
        ],
    )

    def resolve(targets, _secrets=None):
        return {t.key: _answer(MODEL_AVAILABILITY_UNAVAILABLE, "not in catalog") for t in targets}

    stops = check_sprint_availability(config, resolve=resolve)
    dev_stop = next(stop for stop in stops if stop.phase == "dev")
    assert len(dev_stop.excluded) == 2, "two endpoints are two candidates, not one"
    assert dev_stop.models == ["gpt-5", "gpt-5"]


def test_preflight_phase_is_gated_on_what_preflight_actually_dispatches(tmp_path):
    """Preflight runs before routing, so the adaptive pool is not its pool.

    An available dev-pool agent must not clear the preflight phase: the
    preflight call uses the configured preflight profile, and dispatching it
    when the account cannot invoke it is exactly the paid discovery this gate
    exists to prevent.
    """
    config = _config(tmp_path)
    preflight_models = {p.model for p in preflight_dispatch_profiles(config)}

    def resolve(targets, _secrets=None):
        return {
            t.key: _answer(
                MODEL_AVAILABILITY_UNAVAILABLE
                if t.model in preflight_models
                else MODEL_AVAILABILITY_AVAILABLE
            )
            for t in targets
        }

    stops = check_sprint_availability(config, resolve=resolve)
    assert {stop.phase for stop in stops} == {"preflight"}
