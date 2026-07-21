"""Seam: reviewer attempt-completion telemetry from the review pool → routing.

Exercises the full cross-phase flow the story hinges on (#1388, conventions §8):
a reviewer that ALWAYS returns unparseable output is recorded as a failed attempt
at the review invocation boundary, those attempts fold into the derived
completion-rate profile, and after ``min_runs`` the router deprioritizes it below
a parseable-but-otherwise-equivalent reviewer within the same pool.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    PARSE_ERROR_OUTPUT,
    _make_agent_result,
    _make_pool_config,
    _make_task,
)

from theforge.assignment import _select_reviewers
from theforge.config import AgentDef, ModelProfile
from theforge.coordinator.model_profiles_bridge import _extract_reviewer_attempts
from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.state import CoordinatorState, ReviewCycleMetadata
from theforge.model_profiles import RunOutcome, apply_run


def _reviewer(name: str, model: str) -> ModelProfile:
    # CLI transport (cli="claude") avoids API-key auth in selection; distinct
    # models give distinct canonical IDs so completion telemetry keys apart.
    return ModelProfile(
        name=name,
        cli="claude",
        model=model,
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_unparseable_reviewer_deprioritized_after_min_runs(
    mock_pool, _mock_log_agent_result, tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    good = _reviewer("good", "opus")
    bad = _reviewer("bad", "sonnet")
    config = _make_pool_config(tmp_path, [good, bad], good)
    # Disable parse-failure demotion so the always-unparseable reviewer keeps being
    # invoked across every cycle — this test measures the completion-rate signal,
    # not the orthogonal demotion mechanism.
    config = dataclasses.replace(
        config, retry=dataclasses.replace(config.retry, demotion_threshold=0)
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

    min_runs = 5
    # Every cycle: good returns a parseable APPROVE, bad returns unparseable text.
    mock_pool.side_effect = [
        [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="good"),
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="bad"),
        ]
        for _ in range(min_runs)
    ]

    profiles: dict = {"models": {}}
    for _ in range(min_runs):
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
        )
        # Reset the per-cycle attempt buffer and fold this run's attempts into the
        # derived profile — one RunOutcome per run, mirroring end-of-run persistence.
        attempts = _extract_reviewer_attempts(state)
        state.reviewer_attempts = []
        apply_run(
            profiles,
            RunOutcome(
                complexity="medium",
                dev_model="dev",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=0.0,
                reviewer_attempts=attempts,
            ),
        )

    # The unparseable reviewer's attempts were recorded as failures (not dropped).
    bad_review = profiles["models"]["anthropic/sonnet/cli"]["review"]
    good_review = profiles["models"]["anthropic/opus/cli"]["review"]
    assert bad_review["_attempted_count"] == min_runs
    assert bad_review["_completed_count"] == 0
    assert good_review["_completed_count"] == min_runs

    # Routing now deprioritizes the always-unparseable reviewer below the good one,
    # even though both are strong-tier and otherwise equivalent.
    agents = [
        AgentDef("bad", "anthropic", "sonnet", 1.0, 300, "strong", cli="claude"),
        AgentDef("good", "anthropic", "opus", 1.0, 300, "strong", cli="claude"),
    ]
    audit: dict = {}
    selected = _select_reviewers(
        agents,
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        model_profiles=profiles,
        completion_threshold=0.5,
        completion_min_runs=min_runs,
        completion_audit=audit,
    )
    assert [a.name for a in selected] == ["good"]
    assert audit["applied"] is True
    assert audit["deprioritized"] == ["bad"]
