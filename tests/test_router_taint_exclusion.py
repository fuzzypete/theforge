"""Router-consumed aggregates exclude tainted runs — ADR-0006 clause 4 consumer half.

Covers issue #1852: every router-consumed aggregate and profile lookup filters
out runs the trust-status marker flagged ``tainted`` (a run that failed its own
trust checks), while ``trusted`` and ``unchecked`` runs are still consumed, and
the excluded-for-taint count surfaces in the ``routing_decision`` block. The
filter is centralized in :mod:`theforge.coordinator.trust_status`; these tests
exercise the primitive plus each consumer.
"""

from __future__ import annotations

from pathlib import Path

from theforge.config.types import RecencyConfig
from theforge.coordinator import audit_substrate as sub
from theforge.coordinator.trust_status import (
    TRUST_TAINTED,
    TRUST_TRUSTED,
    TRUST_UNCHECKED,
    filter_tainted_records,
    is_routing_admissible,
    is_tainted,
)
from theforge.model_profiles import RunOutcome, apply_run, get_dev_success_rate

# ── Step 1: centralized taint gate ─────────────────────────────────────────


def test_is_tainted_only_for_affirmative_taint() -> None:
    assert is_tainted(TRUST_TAINTED) is True
    assert is_tainted(TRUST_TRUSTED) is False
    assert is_tainted(TRUST_UNCHECKED) is False
    assert is_tainted(None) is False
    assert is_tainted("") is False


def test_is_routing_admissible_is_inverse_of_taint() -> None:
    assert is_routing_admissible(TRUST_TRUSTED) is True
    assert is_routing_admissible(TRUST_UNCHECKED) is True
    assert is_routing_admissible(None) is True
    assert is_routing_admissible(TRUST_TAINTED) is False


def test_filter_tainted_records_partitions_and_counts() -> None:
    records = [
        {"id": 1, "trust_status": TRUST_TRUSTED},
        {"id": 2, "trust_status": TRUST_TAINTED},
        {"id": 3, "trust_status": TRUST_UNCHECKED},
        {"id": 4},  # missing status → admissible
        {"id": 5, "trust_status": TRUST_TAINTED},
    ]
    admissible, excluded = filter_tainted_records(records)
    assert [r["id"] for r in admissible] == [1, 3, 4]
    assert excluded == 2


def test_filter_tainted_records_custom_status_extractor() -> None:
    records = [("a", TRUST_TRUSTED), ("b", TRUST_TAINTED)]
    admissible, excluded = filter_tainted_records(records, status_of=lambda r: r[1])
    assert [r[0] for r in admissible] == ["a"]
    assert excluded == 1


def test_filter_tainted_records_does_not_mutate_source() -> None:
    records = [{"trust_status": TRUST_TAINTED}, {"trust_status": TRUST_TRUSTED}]
    original = [dict(r) for r in records]
    filter_tainted_records(records)
    assert records == original


# ── Step 3: model-profile aggregates exclude tainted ───────────────────────


def _dev_outcome(*, success: bool, tainted: bool) -> RunOutcome:
    return RunOutcome(
        complexity="medium",
        dev_model="sonnet",
        dev_success=success,
        dev_iterations=1,
        dev_cost_usd=0.0,
        dev_tainted=tainted,
    )


def test_dev_success_rate_includes_trusted_and_unchecked() -> None:
    data: dict = {"models": {}}
    # trusted + unchecked map to dev_tainted=False (both admissible).
    for _ in range(2):
        apply_run(data, _dev_outcome(success=True, tainted=False))
    apply_run(data, _dev_outcome(success=False, tainted=False))
    # 2 of 3 admissible runs succeeded. Pinned to recency mode="off" so this
    # taint-admissibility test asserts the cumulative rate directly, decoupled
    # from the recency-weighting curve (#1392) exercised elsewhere.
    assert get_dev_success_rate(
        data, "sonnet", "medium", min_runs=1, recency=RecencyConfig(mode="off")
    ) == round(2 / 3, 4)


def test_dev_success_rate_excludes_tainted() -> None:
    data: dict = {"models": {}}
    apply_run(data, _dev_outcome(success=True, tainted=False))
    # Five tainted failures must not drag the rate down: they don't teach.
    for _ in range(5):
        apply_run(data, _dev_outcome(success=False, tainted=True))
    assert get_dev_success_rate(data, "sonnet", "medium", min_runs=1) == 1.0
    dev = data["models"]["sonnet"]["dev"]
    assert dev["runs"] == 1  # tainted runs excluded from the sample
    assert dev["tainted_runs"] == 5  # but visibly tallied, not deleted
    assert dev["by_complexity"]["medium"]["tainted_runs"] == 5


def test_all_tainted_dev_history_is_cold_start() -> None:
    data: dict = {"models": {}}
    for _ in range(6):
        apply_run(data, _dev_outcome(success=True, tainted=True))
    # No admissible runs → below the sample floor → no ranked rate.
    assert get_dev_success_rate(data, "sonnet", "medium", min_runs=3) is None
    dev = data["models"]["sonnet"]["dev"]
    assert dev.get("runs", 0) == 0
    assert dev["tainted_runs"] == 6


def test_review_and_preflight_aggregates_exclude_tainted() -> None:
    data: dict = {"models": {}}
    tainted = RunOutcome(
        complexity="medium",
        dev_model="sonnet",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.0,
        dev_tainted=True,
        preflight_model="haiku",
        preflight_cost_usd=0.0,
        reviewers={"opus": (2, 3, 0.0)},
    )
    apply_run(data, tainted)
    review = data["models"]["opus"]["review"]
    preflight = data["models"]["haiku"]["preflight"]
    # Reviewer completion + preflight capability aggregates saw zero admissible
    # runs; the tainted cycles/run are tallied under tainted_runs instead.
    assert review.get("runs", 0) == 0
    assert review["tainted_runs"] == 2
    assert preflight.get("runs", 0) == 0
    assert preflight["tainted_runs"] == 1


# ── Step 2: substrate escalation history excludes tainted ───────────────────


def _run_record(
    *,
    run_id: str,
    slug: str,
    success: bool,
    trust_status: str,
    started_at: str,
) -> dict:
    return {
        "run_id": run_id,
        "record_schema_version": sub.CURRENT_RECORD_SCHEMA_VERSION,
        "trust_status": trust_status,
        "trust_checks": [],
        "task": {"slug": slug},
        "outcome": {"success": success, "final_phase": "DONE" if success else "ESCALATE"},
        "timing": {"started_at": started_at, "duration_seconds": 1.0},
        "preflight": {"complexity": "medium", "complexity_score": 5},
        "cost": {
            "total_usd": 1.0,
            "agents": [
                {
                    "phase": "dev",
                    "provider": "anthropic",
                    "model": "sonnet",
                    "cli": "claude",
                    "name": "dev",
                }
            ],
        },
    }


def _seed(conn, records: list[dict]) -> None:
    for record in records:
        sub.upsert_run_record(conn, record, provenance="native")
    conn.commit()


def test_escalation_history_includes_trusted_and_unchecked_excludes_tainted(
    tmp_path: Path,
) -> None:
    from theforge.coordinator.escalation_history import (
        load_escalation_history_with_taint_stats,
    )

    conn = sub.create_or_open(tmp_path)
    try:
        _seed(
            conn,
            [
                _run_record(
                    run_id="r-trusted",
                    slug="s-trusted",
                    success=False,
                    trust_status=TRUST_TRUSTED,
                    started_at="2026-03-01T10:00:00+00:00",
                ),
                _run_record(
                    run_id="r-unchecked",
                    slug="s-unchecked",
                    success=True,
                    trust_status=TRUST_UNCHECKED,
                    started_at="2026-03-02T10:00:00+00:00",
                ),
                _run_record(
                    run_id="r-tainted",
                    slug="s-tainted",
                    success=False,
                    trust_status=TRUST_TAINTED,
                    started_at="2026-03-03T10:00:00+00:00",
                ),
            ],
        )
    finally:
        conn.close()

    history, excluded = load_escalation_history_with_taint_stats(tmp_path)
    slugs = {r.story for r in history}
    assert slugs == {"s-trusted", "s-unchecked"}  # trusted + unchecked kept
    assert "s-tainted" not in slugs  # tainted excluded
    assert excluded == 1  # and counted


def test_tainted_runs_remain_in_substrate(tmp_path: Path) -> None:
    """ADR-0002 refusal-to-forget: filtering is read-time, never deletion."""
    conn = sub.create_or_open(tmp_path)
    try:
        _seed(
            conn,
            [
                _run_record(
                    run_id="r-tainted",
                    slug="s-tainted",
                    success=False,
                    trust_status=TRUST_TAINTED,
                    started_at="2026-03-03T10:00:00+00:00",
                )
            ],
        )
        assert sub.count_records(conn) == 1  # row still present after filtering reads
        loaded = sub.latest_record_for(conn, run_id="r-tainted")
    finally:
        conn.close()
    assert loaded is not None
    assert loaded["trust_status"] == TRUST_TAINTED


# ── Step 4: excluded-for-taint count in routing_decision ───────────────────


def test_routing_decision_records_excluded_for_taint() -> None:
    from theforge.assignment import assign_models
    from theforge.config import AgentDef, AssignmentConfig

    agents = [
        AgentDef(
            name="claude-sonnet",
            provider=None,
            model="sonnet",
            budget_usd=10.0,
            timeout_seconds=300,
            tier="cheap",
            cli="claude",
        )
    ]
    decision = assign_models(
        agents,
        AssignmentConfig(enabled=True, escalation_memory=True),
        "medium",
        complexity_score=5,
        excluded_for_taint=3,
    )
    assert decision.routing_decision["excluded_for_taint"] == 3


def test_routing_decision_excluded_for_taint_defaults_zero() -> None:
    from theforge.assignment import assign_models
    from theforge.config import AgentDef, AssignmentConfig

    agents = [
        AgentDef(
            name="claude-sonnet",
            provider=None,
            model="sonnet",
            budget_usd=10.0,
            timeout_seconds=300,
            tier="cheap",
            cli="claude",
        )
    ]
    decision = assign_models(
        agents,
        AssignmentConfig(enabled=True),
        "medium",
        complexity_score=5,
    )
    assert decision.routing_decision["excluded_for_taint"] == 0


# ── Step 5: coordinator preflight seam end-to-end ──────────────────────────


def test_preflight_seam_excludes_tainted_and_records_count(tmp_path: Path) -> None:
    """The real preflight assignment seam consumes trusted/unchecked history,
    ignores a tainted record, and the audit-visible routing_decision carries the
    excluded-for-taint count."""
    from dataclasses import replace

    from coord_test_helpers import _make_config

    from theforge.config import AgentDef, AssignmentConfig
    from theforge.coordinator.preflight import _apply_preflight_config
    from theforge.coordinator.state import CoordinatorState

    # Seed history: two admissible ESCALATE rows for the same model (enough to
    # feed promotion) plus one tainted row that must be ignored.
    conn = sub.create_or_open(tmp_path)
    try:
        _seed(
            conn,
            [
                _run_record(
                    run_id="r-trusted",
                    slug="s-trusted",
                    success=False,
                    trust_status=TRUST_TRUSTED,
                    started_at="2026-03-01T10:00:00+00:00",
                ),
                _run_record(
                    run_id="r-unchecked",
                    slug="s-unchecked",
                    success=False,
                    trust_status=TRUST_UNCHECKED,
                    started_at="2026-03-02T10:00:00+00:00",
                ),
                _run_record(
                    run_id="r-tainted",
                    slug="s-tainted",
                    success=False,
                    trust_status=TRUST_TAINTED,
                    started_at="2026-03-03T10:00:00+00:00",
                ),
            ],
        )
    finally:
        conn.close()

    config = replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="claude-sonnet",
                provider=None,
                model="sonnet",
                budget_usd=10.0,
                timeout_seconds=300,
                tier="cheap",
                cli="claude",
            )
        ],
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=True,
            max_cost_per_story_usd=None,
        ),
    )
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5

    _apply_preflight_config(config, state)

    block = state.routing_decision
    assert isinstance(block, dict)
    # Only the single tainted record was set aside; the two admissible rows fed
    # the router's history.
    assert block["excluded_for_taint"] == 1
