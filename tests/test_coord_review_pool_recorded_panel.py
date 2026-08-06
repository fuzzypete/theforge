"""The panel recorded against a cycle is the panel that actually ran.

Review-cycle pricing joins observed cost to ``reviews[].pool_models`` to learn
what a given panel costs. That join is only sound if the recorded panel is the
one that was attempted: a demoted reviewer never runs and never bills, so a
cycle costed at two reviewers must not be recorded as three, or a smaller
panel's spend is attributed to a larger one and every future story seated on
that larger panel is under-reserved.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    _make_agent_result,
    _make_pool_config,
    _make_review_profile,
    _make_task,
)

from theforge.coordinator import story_budget as sb
from theforge.coordinator.audit_render import build_reviews
from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.state import CoordinatorState, ReviewCycleMetadata

_APPROVE = (
    "```yaml\nverdict: APPROVE\nsummary: ok\nfindings: []\n"
    "story_compliance:\n  matches_spec: true\n"
    "test_coverage:\n  adequate: true\n```"
)


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_demoted_reviewer_is_absent_from_the_recorded_panel(mock_pool, _mock_log, tmp_path):
    """Configured A+B+C, C demoted → the cycle is recorded as A+B."""
    alpha = _make_review_profile("alpha")
    bravo = _make_review_profile("bravo")
    charlie = _make_review_profile("charlie")
    config = _make_pool_config(tmp_path, [alpha, bravo, charlie], alpha)
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")
    state.reviewer_demoted.add("charlie")

    mock_pool.return_value = [
        _make_agent_result(success=True, output=_APPROVE, profile_name="alpha"),
        _make_agent_result(success=True, output=_APPROVE, profile_name="bravo"),
    ]

    meta = _meta()
    _run_review_pool(
        state,
        config,
        task,
        "story",
        workspace,
        "branch",
        meta,
        notify=False,
        enforce_budgets=False,
    )

    assert meta.pool_models == ["alpha", "bravo"]
    assert "charlie" not in meta.pool_models

    # The audit is what downstream pricing actually reads.
    state.review_cycle_metadata.append(meta)
    rendered = build_reviews(state)
    assert rendered[0]["cycle"] == 1
    assert rendered[0]["pool_models"] == ["alpha", "bravo"]


def test_recorded_panel_governs_pricing_for_the_panel_that_ran(tmp_path):
    """Three A+B cycles price A+B — they are not banked against A+B+C."""
    from test_story_budget_allocation import _record, _seed_substrate

    _seed_substrate(
        tmp_path,
        [
            _record(
                run_id="demoted",
                score=5,
                cost=6.0,
                review_cycle_costs=[2.00, 2.00, 2.00],
                review_pools=[["alpha", "bravo"]] * 3,
            ),
        ],
    )

    ran = sb.derive_review_cycle_planning_price(
        tmp_path, configured_ceiling_usd=17.55, composition=["alpha", "bravo"]
    )
    assert ran.basis == sb.BASIS_OBSERVED_COMPOSITION
    assert ran.planned_cost_usd == round(2.00 * 1.25, 4)

    # The panel that never ran has no history of its own and falls to the
    # broader population rather than inheriting the cheaper panel's price.
    never_ran = sb.derive_review_cycle_planning_price(
        tmp_path, configured_ceiling_usd=17.55, composition=["alpha", "bravo", "charlie"]
    )
    assert never_ran.basis == sb.BASIS_OBSERVED_REVIEW_CYCLE
    assert never_ran.composition == ("alpha", "bravo", "charlie")
