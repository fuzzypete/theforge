"""Integration test for the review_pool → provider_health.yaml seam.

The review_pool records a provider-health entry for each reviewer that
returned a provider-shape failure.  This test exercises that wiring against
the real `_run_review_pool` entry point so the recording side stays glued
to the failing exit shape the adaptive router will later consume.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    _make_agent_result,
    _make_pool_config,
    _make_review_profile,
    _make_task,
)

from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.state import CoordinatorState, ReviewCycleMetadata
from theforge.provider_health import load_provider_health


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_provider_capacity_failure_recorded_to_disk(mock_pool, _mock_log, tmp_path):
    """Reviewer returns capacity error → record lands in .forge/provider_health.yaml."""
    flapping = _make_review_profile("openai-gpt-5.5")
    healthy = _make_review_profile("opus")
    config = _make_pool_config(tmp_path, [flapping, healthy], healthy)
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

    mock_pool.return_value = [
        _make_agent_result(
            success=False,
            output="ERROR: Selected model is at capacity. Please try a different model.",
            profile_name="openai-gpt-5.5",
        ),
        _make_agent_result(
            success=True,
            output=(
                "```yaml\nverdict: APPROVE\nsummary: ok\nfindings: []\n"
                "story_compliance:\n  matches_spec: true\n"
                "test_coverage:\n  adequate: true\n```"
            ),
            profile_name="opus",
        ),
    ]

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

    records = load_provider_health(tmp_path / ".forge" / "provider_health.yaml")
    assert len(records) == 1
    assert records[0].model == "openai-gpt-5.5"
    assert records[0].signature == "capacity"


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_runtime_failure_not_recorded(mock_pool, _mock_log, tmp_path):
    """A non-provider-shape failure (parse/runtime) must NOT poison the health signal."""
    p = _make_review_profile("r1")
    config = _make_pool_config(tmp_path, [p, _make_review_profile("r2")], p)
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

    mock_pool.return_value = [
        _make_agent_result(
            success=False,
            output="AssertionError: unrelated runtime bug in agent loop",
            profile_name="r1",
        ),
        _make_agent_result(
            success=True,
            output=(
                "```yaml\nverdict: APPROVE\nsummary: ok\nfindings: []\n"
                "story_compliance:\n  matches_spec: true\n"
                "test_coverage:\n  adequate: true\n```"
            ),
            profile_name="r2",
        ),
    ]

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

    health_path = tmp_path / ".forge" / "provider_health.yaml"
    if health_path.exists():
        # File may exist if the directory was touched, but no record should have been appended.
        assert load_provider_health(health_path) == []
