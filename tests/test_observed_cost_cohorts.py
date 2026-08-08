"""Observed-cost cohort projection off the invocation ledger (#2206).

The final assignment tie-break compares spend only within like-for-like work.
These tests pin the substrate reader that produces those cohorts: what it keys
them on, and — more importantly — what it refuses to count. An observation the
reader admits without a complete ledger is an average over unlike work, which is
the confounded comparison the story exists to avoid.
"""

from __future__ import annotations

import json
from pathlib import Path

from theforge.coordinator import audit_substrate as sub

_STAMP = "2026-08-01T12:00:00+00:00"


def _ledger(
    *,
    role: str = "dev",
    complexity: str = "MEDIUM",
    effort: str | None = "high",
    cost: float | None = 2.1,
    identity: str = "anthropic/opus/cli",
    provenance: str | None = "provider_reported",
    usage: dict | None = None,
) -> dict:
    provider, model, transport = identity.split("/")
    return {
        "version": 1,
        "role": role,
        "profile": "dev",
        "complexity": complexity,
        "complexity_score": 5,
        "reasoning_effort": effort,
        "cost_usd": cost,
        "cost_provenance": provenance,
        "configured_identity": {
            "raw": model,
            "identity": identity,
            "resolution": "canonical",
            "transport": transport,
        },
        "resolved_primary_identity": {
            "raw": model,
            "identity": identity,
            "resolution": "canonical",
            "transport": transport,
        },
        "configured_differs_from_resolved": False,
        "usage": usage if usage is not None else {"input_tokens": 100, "output_tokens": 50},
        "billed_components": [],
    }


def _record(
    *,
    run_id: str,
    ledgers: list[dict | None],
    trust_status: str | None = None,
    started_at: str = _STAMP,
) -> dict:
    """An audit record carrying ``cost.agents`` entries.

    A ``None`` ledger models a pre-#2205 entry: the agent is recorded but the
    ledger block never existed, so ``full_ledger`` is False.
    """
    agents: list[dict] = []
    for index, ledger in enumerate(ledgers):
        entry: dict = {"role": "dev", "profile": f"agent-{index}"}
        if ledger is None:
            entry["cost_usd"] = 9.99
            entry["model_used"] = "opus"
        else:
            entry["ledger"] = ledger
            entry["cost_usd"] = ledger.get("cost_usd")
        agents.append(entry)
    rec: dict = {
        "run_id": run_id,
        "task": {"slug": f"story-{run_id}", "name": run_id},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": started_at, "duration_seconds": 60.0},
        "cost": {"total_usd": 3.0, "agents": agents},
        "totals": {"cost_usd": 3.0, "duration_s": 60.0},
        "preflight": {"complexity": "medium", "complexity_score": 5},
        "reviews": [],
    }
    if trust_status is not None:
        rec["trust_status"] = trust_status
    return rec


def _seed(project_root: Path, records: list[dict]) -> None:
    runs = sub.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    for rec in records:
        (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    conn = sub.create_or_open(project_root)
    try:
        for rec in records:
            sub.upsert_run_record(conn, rec, provenance="native")
        conn.commit()
    finally:
        conn.close()


def _cohorts(project_root: Path, records: list[dict], stats: dict | None = None) -> dict:
    _seed(project_root, records)
    conn = sub.require_substrate(project_root)
    try:
        return sub.derive_observed_cost_cohorts(conn, stats=stats)
    finally:
        conn.close()


class TestCohortKeying:
    def test_observations_group_by_role_complexity_and_effort(self, tmp_path: Path) -> None:
        """Three dimensions make the key; unlike work lands in separate cohorts."""
        cohorts = _cohorts(
            tmp_path,
            [
                _record(run_id="r1", ledgers=[_ledger(cost=2.0)]),
                _record(run_id="r2", ledgers=[_ledger(cost=2.2)]),
                # Same model and role, different effort → a different cohort.
                _record(run_id="r3", ledgers=[_ledger(cost=8.0, effort="low")]),
                # Same model and effort, different complexity → different again.
                _record(run_id="r4", ledgers=[_ledger(cost=9.0, complexity="HIGH")]),
            ],
        )
        opus = cohorts["anthropic/opus/cli"]
        assert sorted(opus) == ["DEV|HIGH|high", "DEV|MEDIUM|high", "DEV|MEDIUM|low"]
        assert [o["cost_usd"] for o in opus["DEV|MEDIUM|high"]["observations"]] == [2.0, 2.2]
        assert opus["DEV|MEDIUM|high"]["reasoning_effort"] == "high"
        # The expensive low-effort run does not contaminate the high-effort cohort.
        assert [o["cost_usd"] for o in opus["DEV|MEDIUM|low"]["observations"]] == [8.0]

    def test_each_model_keeps_its_own_cohorts(self, tmp_path: Path) -> None:
        cohorts = _cohorts(
            tmp_path,
            [
                _record(
                    run_id="r1",
                    ledgers=[
                        _ledger(cost=2.1, identity="anthropic/opus/cli"),
                        _ledger(cost=0.4, identity="openai/gpt-5.4/api"),
                    ],
                )
            ],
        )
        assert (
            cohorts["anthropic/opus/cli"]["DEV|MEDIUM|high"]["observations"][0]["cost_usd"] == 2.1
        )
        assert (
            cohorts["openai/gpt-5.4/api"]["DEV|MEDIUM|high"]["observations"][0]["cost_usd"] == 0.4
        )

    def test_observation_carries_its_stamp_and_provenance(self, tmp_path: Path) -> None:
        """Recency and completeness are judged downstream, so both travel."""
        cohorts = _cohorts(tmp_path, [_record(run_id="r1", ledgers=[_ledger()])])
        obs = cohorts["anthropic/opus/cli"]["DEV|MEDIUM|high"]["observations"][0]
        assert obs["started_at"] == _STAMP
        assert obs["cost_provenance"] == "provider_reported"


class TestIncompleteObservationsAreRefused:
    def test_pre_ledger_entry_is_not_an_observation(self, tmp_path: Path) -> None:
        """A legacy entry has a cost but no cohort — counting it invents a cohort."""
        assert _cohorts(tmp_path, [_record(run_id="r1", ledgers=[None])]) == {}

    def test_entry_without_usage_is_refused(self, tmp_path: Path) -> None:
        """No usage means the cost was never measured against real work."""
        assert _cohorts(tmp_path, [_record(run_id="r1", ledgers=[_ledger(usage={})])]) == {}

    def test_entry_without_cost_is_refused(self, tmp_path: Path) -> None:
        assert _cohorts(tmp_path, [_record(run_id="r1", ledgers=[_ledger(cost=None)])]) == {}

    def test_entry_without_cost_provenance_is_refused(self, tmp_path: Path) -> None:
        """An unattributed cost is not evidence of what the work cost."""
        assert _cohorts(tmp_path, [_record(run_id="r1", ledgers=[_ledger(provenance=None)])]) == {}

    def test_entry_without_reasoning_effort_is_refused(self, tmp_path: Path) -> None:
        """Effort is a cohort dimension; without it there is no cohort to join."""
        assert _cohorts(tmp_path, [_record(run_id="r1", ledgers=[_ledger(effort=None)])]) == {}

    def test_complete_entries_survive_alongside_refused_ones(self, tmp_path: Path) -> None:
        """One bad entry does not discard the good entry beside it."""
        cohorts = _cohorts(
            tmp_path,
            [_record(run_id="r1", ledgers=[_ledger(effort=None), None, _ledger(cost=3.3)])],
        )
        assert [
            o["cost_usd"] for o in cohorts["anthropic/opus/cli"]["DEV|MEDIUM|high"]["observations"]
        ] == [3.3]


class TestTaint:
    def test_tainted_runs_do_not_teach_what_work_costs(self, tmp_path: Path) -> None:
        """ADR-0006 clause 4: tainted history is excluded and the count is reported."""
        stats: dict = {}
        cohorts = _cohorts(
            tmp_path,
            [
                _record(run_id="r1", ledgers=[_ledger(cost=2.0)]),
                _record(run_id="r2", ledgers=[_ledger(cost=99.0)], trust_status="tainted"),
            ],
            stats=stats,
        )
        assert [
            o["cost_usd"] for o in cohorts["anthropic/opus/cli"]["DEV|MEDIUM|high"]["observations"]
        ] == [2.0]
        assert stats["excluded_for_taint"] == 1


class TestReaderSelectorSeam:
    """The reader writes keys the selector must find, or cost is silently inert.

    The two sides derive the storage key independently — the reader from the
    ledger's canonical identity, the selector from the candidate's runtime
    identity fields. If those spellings ever diverge the lookup does not error;
    it falls back to the declared seed forever, and every assertion about the
    tie-break still passes while the feature does nothing.
    """

    def test_derived_cohorts_are_findable_by_the_assignment_lookup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime  # noqa: PLC0415

        from theforge.model_profiles import get_observed_cost_tiebreak_signal  # noqa: PLC0415

        cohorts = _cohorts(
            tmp_path,
            [
                _record(run_id="r1", ledgers=[_ledger(cost=2.0)]),
                _record(run_id="r2", ledgers=[_ledger(cost=2.1)]),
                _record(run_id="r3", ledgers=[_ledger(cost=2.2)]),
            ],
        )
        signal = get_observed_cost_tiebreak_signal(
            cohorts,
            "opus",
            role="dev",
            complexity="MEDIUM",
            reasoning_effort="high",
            seed=75.0,
            # The identity an AgentDef for this model carries at selection time.
            actual_model="opus",
            provider="anthropic",
            cli="claude",
            now=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        )
        assert signal["source"] == "observed"
        assert signal["observations"] == 3
        assert signal["value"] == 2.1  # median of the derived cohort

    def test_effort_mismatch_at_selection_falls_back_to_the_seed(self, tmp_path: Path) -> None:
        """Recorded high-effort spend must not price a low-effort request."""
        from datetime import UTC, datetime  # noqa: PLC0415

        from theforge.model_profiles import get_observed_cost_tiebreak_signal  # noqa: PLC0415

        cohorts = _cohorts(
            tmp_path,
            [_record(run_id=f"r{i}", ledgers=[_ledger(cost=2.0)]) for i in range(3)],
        )
        signal = get_observed_cost_tiebreak_signal(
            cohorts,
            "opus",
            role="dev",
            complexity="MEDIUM",
            reasoning_effort="low",
            seed=75.0,
            actual_model="opus",
            provider="anthropic",
            cli="claude",
            now=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        )
        assert signal["source"] == "seed"
        assert signal["value"] == 75.0


class TestLoader:
    def test_fresh_repo_yields_no_cohorts_rather_than_an_error(self, tmp_path: Path) -> None:
        cohorts, excluded = sub.load_observed_cost_cohorts(tmp_path)
        assert cohorts == {}
        assert excluded == 0

    def test_loader_reads_cohorts_and_reports_taint(self, tmp_path: Path) -> None:
        _seed(
            tmp_path,
            [
                _record(run_id="r1", ledgers=[_ledger(cost=2.0)]),
                _record(run_id="r2", ledgers=[_ledger(cost=99.0)], trust_status="tainted"),
            ],
        )
        cohorts, excluded = sub.load_observed_cost_cohorts(tmp_path)
        assert cohorts["anthropic/opus/cli"]["DEV|MEDIUM|high"]["observations"] == [
            {"cost_usd": 2.0, "started_at": _STAMP, "cost_provenance": "provider_reported"}
        ]
        assert excluded == 1
