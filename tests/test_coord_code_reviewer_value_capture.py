"""Code-reviewer value capture at review-pool completion (#2156).

Seam test for the producer half of the code-review value signal: the coordinator
computes per-reviewer blocking-finding uniqueness at code-review pool completion,
writes it to ``state.code_reviewer_value``, and the model-profiles bridge folds it
into the ``code_review_value`` profile section — separately from the plan-review
section — so the router has something to read at the next story's selection.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from tests.coord_test_helpers import (
    DEFAULT_REVIEW_PROFILE,
    _make_agent_result,
    _make_pool_config,
    _make_task,
)
from theforge.coordinator.model_profiles_bridge import _extract_code_reviewer_values
from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.state import CoordinatorState, ReviewCycleMetadata
from theforge.model_profiles import RunOutcome, apply_run
from theforge.reviewer_value import CODE_SECTION, SECTION


def _review(observed: str, evidence: str) -> str:
    return (
        "```yaml\n"
        "verdict: REQUEST_CHANGES\n"
        'summary: "Problems found."\n'
        "findings:\n"
        "  - severity: P1\n"
        "    file: src/foo.py\n"
        "    line: 10\n"
        f'    observed: "{observed}"\n'
        '    expected: "Behaviour conforms to the project contract."\n'
        f'    evidence: "{evidence}"\n'
        '    suggestion: "Fix it"\n'
        "story_compliance:\n"
        "  matches_spec: false\n"
        "  mismatches:\n"
        '    - "does not match"\n'
        "test_coverage:\n"
        "  adequate: false\n"
        "  gaps:\n"
        '    - "no test"\n'
        "ac_verification:\n"
        '  - criterion: "Implementation satisfies the spec"\n'
        "    status: NOT_VERIFIED\n"
        '    evidence: "see findings"\n'
        "```\n"
    )


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)


def _profiles():
    a = dataclasses.replace(DEFAULT_REVIEW_PROFILE, name="rev-a")
    b = dataclasses.replace(DEFAULT_REVIEW_PROFILE, name="rev-b")
    return a, b


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_completed_code_review_pool_populates_code_reviewer_value(mock_pool, _log, tmp_path):
    prof_a, prof_b = _profiles()
    config = _make_pool_config(tmp_path, [prof_a, prof_b], synthesis=None)
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")
    state.preflight_complexity = "medium"

    # rev-a raises a blocking finding anchored on ``retry_budget``; rev-b raises a
    # different one anchored on ``session_cache``. Neither corroborates the other,
    # so both are unique — the deterministic anchor-overlap the router consumes.
    mock_pool.return_value = [
        _make_agent_result(
            success=True,
            output=_review("The retry budget is ignored.", "retry_budget is never read."),
            profile_name="rev-a",
        ),
        _make_agent_result(
            success=True,
            output=_review("The session cache never expires.", "session_cache grows forever."),
            profile_name="rev-b",
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

    rows = {v["reviewer"]: v for v in state.code_reviewer_value}
    assert set(rows) == {"rev-a", "rev-b"}
    for row in rows.values():
        assert row["cycle"] == 1
        assert row["complexity"] == "medium"
        assert row["total_p1_count"] == 1
        assert row["unique_p1_count"] == 1
        assert row["parse_error_count"] == 0
        assert row["latency_s"] is not None


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_corroborated_finding_is_not_counted_unique(mock_pool, _log, tmp_path):
    prof_a, prof_b = _profiles()
    config = _make_pool_config(tmp_path, [prof_a, prof_b], synthesis=None)
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")
    state.preflight_complexity = "medium"

    # Both reviewers anchor on ``retry_budget`` — the same issue, independently
    # surfaced, so neither reviewer's finding is unique.
    mock_pool.return_value = [
        _make_agent_result(
            success=True,
            output=_review("The retry budget is ignored.", "retry_budget is never read."),
            profile_name="rev-a",
        ),
        _make_agent_result(
            success=True,
            output=_review("Retries run unbounded.", "retry_budget has no effect."),
            profile_name="rev-b",
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

    rows = {v["reviewer"]: v for v in state.code_reviewer_value}
    assert rows["rev-a"]["total_p1_count"] == 1
    assert rows["rev-a"]["unique_p1_count"] == 0
    assert rows["rev-b"]["unique_p1_count"] == 0


def test_captured_rows_fold_into_the_code_review_section_only():
    """The capture → bridge → profile-fold seam lands in code_review_value."""
    state = CoordinatorState(review_cycle=0)
    state.code_reviewer_value = [
        {
            "cycle": 1,
            "reviewer": "rev-a",
            "complexity": "medium",
            "unique_p1_count": 0,
            "total_p1_count": 2,
            "latency_s": 120.0,
            "parse_error_count": 0,
            "actual_model": "sonnet",
            "provider": "anthropic",
            "cli": None,
        }
    ]

    samples = _extract_code_reviewer_values(state)
    assert [s.name for s in samples] == ["rev-a"]
    assert samples[0].uniqueness_rate() == 0.0
    assert samples[0].latency_per_p1() == 60.0

    data = apply_run(
        {"models": {}},
        RunOutcome(
            dev_model="dev",
            complexity="medium",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=0.0,
            code_reviewer_values=samples,
        ),
    )
    # _ensure_model keys the entry by canonical identity, not the profile name;
    # the dev model has its own entry, which must carry no reviewer value section.
    (entry,) = [e for e in data["models"].values() if CODE_SECTION in e]
    assert SECTION not in entry
    assert entry[CODE_SECTION]["runs"] == 1
    assert entry[CODE_SECTION]["by_complexity"]["medium"]["avg_uniqueness_rate"] == 0.0
    assert entry[CODE_SECTION]["by_complexity"]["medium"]["avg_latency_per_p1"] == 60.0
