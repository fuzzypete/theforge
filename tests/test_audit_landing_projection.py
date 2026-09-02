"""The landing-evidence projection in the audit substrate (#2849).

Landing evidence is published one artifact per observation under
``.forge/audits/landing``, each carrying its own ``observed_at`` recorded by
whichever observer saw the landing — routinely long after the run record was
written. Until this projection existed nothing indexed those artifacts, so the
substrate answered landed questions from ``audit_records.landing_status``: a
completion-time snapshot taken *before* a queued pull request resolves, in which
"nobody has looked yet" and "did not land" are the same value.

These tests pin the three properties that separates the two:

1. the landed query reads the evidence, not the flattened column;
2. absence of evidence is ``unresolved`` and stays distinct from ``not_landed``;
3. a substrate rebuilt from scratch and one kept current incrementally give the
   same answer, because both project from the same files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theforge.coordinator import audit_read_model, audit_storage, landing_evidence
from theforge.coordinator import audit_substrate as sub


def _record(
    *,
    run_id: str,
    slug: str = "demo",
    started_at: str = "2026-03-01T10:00:00+00:00",
    finished_at: str | None = "2026-03-01T10:30:00+00:00",
    landing_status: str | None = None,
    reviews: list[dict] | None = None,
) -> dict:
    record: dict = {
        "run_id": run_id,
        "task": {"slug": slug, "name": slug},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": started_at, "duration_seconds": 60.0},
        "cost": {"total_usd": 1.0},
        "totals": {"cost_usd": 1.0, "duration_s": 60.0},
        "iterations": {"dev_iterations": 1, "review_cycles": 1},
        "reviews": reviews or [],
        "phases": {"dev": {"cost_usd": 1.0, "duration_s": 60.0, "outcome": "success"}},
    }
    if finished_at is not None:
        record["timing"]["finished_at"] = finished_at
    if landing_status is not None:
        record["landing_status"] = landing_status
    return record


def _write_runs(project_root: Path, records: list[dict]) -> None:
    runs = audit_storage.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    for record in records:
        (runs / f"{record['run_id']}.json").write_text(json.dumps(record), encoding="utf-8")


def _assertion(
    *,
    run_id: str,
    slug: str = "demo",
    observed_at: str = "2026-03-01T12:00:00+00:00",
    landing_mode: str = "merge-pr",
    target_branch: str = "release/v0.16",
    observer: str = "sprint.queued-pr",
    carrier_ref: str = "#2756",
) -> dict:
    return landing_evidence.build_landing_assertion(
        run_id=run_id,
        slug=slug,
        landing_mode=landing_mode,
        target_branch=target_branch,
        reviewed_commit="reviewed-sha",
        gated_commit="gated-sha",
        carrier_kind="pull_request",
        carrier_ref=carrier_ref,
        landed_commit="landed-sha",
        observer=observer,
        observed_at=observed_at,
    )


def _attempt(
    *,
    run_id: str,
    outcome: str,
    slug: str = "demo",
    observed_at: str = "2026-03-01T11:00:00+00:00",
    landing_mode: str = "merge-pr",
    target_branch: str = "release/v0.16",
    observer: str = "sprint.queued-pr",
) -> dict:
    return landing_evidence.build_landing_attempt(
        run_id=run_id,
        slug=slug,
        landing_mode=landing_mode,
        target_branch=target_branch,
        outcome=outcome,
        observer=observer,
        observed_at=observed_at,
    )


def _states_by_run(project_root: Path) -> dict[str, dict]:
    conn = audit_storage.require_substrate(project_root)
    try:
        return {row["run_id"]: row for row in audit_read_model.landing_states(conn)}
    finally:
        conn.close()


class TestLandedQueryReadsEvidence:
    """AC1: the landed answer comes from the assertion, not the flat column."""

    def test_assertion_lands_a_run_whose_flattened_column_says_otherwise(
        self, tmp_path: Path
    ) -> None:
        """The exact shape the story describes.

        The record was written at completion with ``landing_status`` unset (the
        pull request was still queued); the assertion was published an hour and
        a half later when the merge was observed. The substrate must answer from
        the assertion.
        """
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        state = _states_by_run(tmp_path)["r1"]

        assert state["landing_state"] == "landed"
        assert state["flattened_landing_status"] is None
        assert state["observed_at"] == "2026-03-01T12:00:00+00:00"
        assert state["landed_commit"] == "landed-sha"

    def test_flattened_false_does_not_override_a_published_assertion(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1", landing_status="not_landed")])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        state = _states_by_run(tmp_path)["r1"]

        assert state["landing_state"] == "landed"
        assert state["flattened_landing_status"] == "not_landed"

    def test_has_review_approve_require_landed_follows_the_projection(
        self, tmp_path: Path
    ) -> None:
        """The landed query re-pointed by this story.

        ``r1`` carries the assertion but no flattened value; ``r2`` carries
        ``landing_status='landed'`` and no evidence. Under the old column filter
        the answer was ``r2``; under the projection it is ``r1``.
        """
        approve = [{"cycle": 1, "verdict": "APPROVE"}]
        _write_runs(
            tmp_path,
            [
                _record(run_id="r1", reviews=approve),
                _record(run_id="r2", reviews=approve, landing_status="landed"),
            ],
        )
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        conn = audit_storage.require_substrate(tmp_path)
        try:
            landed = list(
                audit_read_model.has_review_approve_in_substrate(conn, "demo", require_landed=True)
            )
            everything = list(audit_read_model.has_review_approve_in_substrate(conn, "demo"))
        finally:
            conn.close()

        assert [record["run_id"] for record in landed] == ["r1"]
        assert {record["run_id"] for record in everything} == {"r1", "r2"}

    def test_landed_run_ids_reports_only_asserted_runs(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1"), _record(run_id="r2", slug="other")])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))
        landing_evidence.write_landing_attempt(
            tmp_path, _attempt(run_id="r2", slug="other", outcome="failed")
        )

        conn = audit_storage.require_substrate(tmp_path)
        try:
            assert audit_read_model.landed_run_ids_in_substrate(conn) == {"r1"}
        finally:
            conn.close()


class TestAbsenceIsNotFailure:
    """AC1: unresolved and not_landed are different answers, everywhere."""

    def test_no_evidence_at_all_is_unresolved(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1")])

        state = _states_by_run(tmp_path)["r1"]

        assert state["landing_state"] == "unresolved"
        assert state["observed_at"] is None
        assert state["last_attempt"] is None

    @pytest.mark.parametrize("outcome", sorted(landing_evidence.RESOLVED_NON_LANDING_OUTCOMES))
    def test_a_resolved_attempt_is_not_landed(self, tmp_path: Path, outcome: str) -> None:
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing_evidence.write_landing_attempt(tmp_path, _attempt(run_id="r1", outcome=outcome))

        state = _states_by_run(tmp_path)["r1"]

        assert state["landing_state"] == "not_landed"
        assert state["last_attempt"]["outcome"] == outcome

    @pytest.mark.parametrize("outcome", sorted(landing_evidence.OPEN_ATTEMPT_OUTCOMES))
    def test_an_open_attempt_stays_unresolved(self, tmp_path: Path, outcome: str) -> None:
        """A queued pull request is not a failure to land — it is not yet an answer."""
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing_evidence.write_landing_attempt(tmp_path, _attempt(run_id="r1", outcome=outcome))

        state = _states_by_run(tmp_path)["r1"]

        assert state["landing_state"] == "unresolved"
        assert state["last_attempt"]["outcome"] == outcome

    def test_a_later_assertion_supersedes_an_earlier_failed_attempt(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing_evidence.write_landing_attempt(tmp_path, _attempt(run_id="r1", outcome="failed"))
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        assert _states_by_run(tmp_path)["r1"]["landing_state"] == "landed"

    def test_three_states_are_distinct_values_not_a_nullable_boolean(self, tmp_path: Path) -> None:
        _write_runs(
            tmp_path,
            [
                _record(run_id="landed-run"),
                _record(run_id="failed-run", slug="b"),
                _record(run_id="silent-run", slug="c"),
            ],
        )
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="landed-run"))
        landing_evidence.write_landing_attempt(
            tmp_path, _attempt(run_id="failed-run", slug="b", outcome="closed")
        )

        states = _states_by_run(tmp_path)

        assert states["landed-run"]["landing_state"] == "landed"
        assert states["failed-run"]["landing_state"] == "not_landed"
        assert states["silent-run"]["landing_state"] == "unresolved"

    def test_a_corrupt_artifact_leaves_the_run_unresolved(self, tmp_path: Path) -> None:
        """A file that is not evidence must not become a landing, or an exception."""
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing = landing_evidence.landing_evidence_dir(tmp_path)
        landing.mkdir(parents=True)
        (landing / "r1.landed.json").write_text("{not json", encoding="utf-8")

        assert _states_by_run(tmp_path)["r1"]["landing_state"] == "unresolved"

    def test_projection_matches_the_filesystem_read_model_run_for_run(
        self, tmp_path: Path
    ) -> None:
        """The SQL answer and ``landing_evidence.landing_state`` must not diverge."""
        _write_runs(
            tmp_path,
            [_record(run_id=f"r{i}", slug=f"s{i}") for i in range(1, 5)],
        )
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1", slug="s1"))
        landing_evidence.write_landing_attempt(
            tmp_path, _attempt(run_id="r2", slug="s2", outcome="queued")
        )
        landing_evidence.write_landing_attempt(
            tmp_path, _attempt(run_id="r3", slug="s3", outcome="timeout")
        )

        states = _states_by_run(tmp_path)

        for run_id in ("r1", "r2", "r3", "r4"):
            assert states[run_id]["landing_state"] == landing_evidence.landing_state(
                tmp_path, run_id
            )


class TestObservedAtAndIntervals:
    """AC2: the assertion's own observed_at, joined to the run's own timing."""

    def test_interval_is_derivable_when_both_endpoints_exist(self, tmp_path: Path) -> None:
        _write_runs(
            tmp_path,
            [_record(run_id="r1", finished_at="2026-03-01T10:30:00+00:00")],
        )
        landing_evidence.write_landing_assertion(
            tmp_path, _assertion(run_id="r1", observed_at="2026-03-01T12:00:00+00:00")
        )

        state = _states_by_run(tmp_path)["r1"]

        assert state["run_finished_at"] == "2026-03-01T10:30:00+00:00"
        assert state["observed_at"] == "2026-03-01T12:00:00+00:00"
        assert state["landing_interval"]["state"] == "resolved"
        assert state["landing_interval"]["seconds"] == pytest.approx(90 * 60)

    def test_missing_landing_endpoint_reports_unresolved_not_zero(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1")])

        interval = _states_by_run(tmp_path)["r1"]["landing_interval"]

        assert interval["state"] == "unresolved"
        assert interval["seconds"] is None
        assert interval["missing_endpoints"] == ["landing_observed_at"]

    def test_missing_run_endpoint_reports_unresolved(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1", finished_at=None)])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        interval = _states_by_run(tmp_path)["r1"]["landing_interval"]

        assert interval["state"] == "unresolved"
        assert interval["seconds"] is None
        assert interval["missing_endpoints"] == ["run_finished_at"]

    def test_no_endpoint_is_borrowed_from_a_neighbouring_run(self, tmp_path: Path) -> None:
        """The run without evidence must not inherit its neighbour's timestamp."""
        _write_runs(tmp_path, [_record(run_id="r1"), _record(run_id="r2", slug="other")])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        states = _states_by_run(tmp_path)

        assert states["r1"]["observed_at"] == "2026-03-01T12:00:00+00:00"
        assert states["r2"]["observed_at"] is None
        assert states["r2"]["landing_interval"]["seconds"] is None

    def test_run_identity_travels_with_the_assertion(self, tmp_path: Path) -> None:
        """Evidence for a run with no indexed record is still queryable."""
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="orphan"))
        _write_runs(tmp_path, [_record(run_id="r1")])

        conn = audit_storage.require_substrate(tmp_path)
        try:
            state = audit_read_model.landing_state_for_run(conn, "orphan")
            assert audit_read_model.landing_state_for_run(conn, "never-heard-of") is None
        finally:
            conn.close()

        assert state["run_id"] == "orphan"
        assert state["landing_state"] == "landed"
        assert state["run_finished_at"] is None
        assert state["landing_interval"]["missing_endpoints"] == ["run_finished_at"]


class TestUnrecognisedValuesProjectVerbatim:
    """AC4: the projection is over the artifact's fields, not over a value set."""

    def test_unknown_mode_observer_and_branch_round_trip(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing_evidence.write_landing_assertion(
            tmp_path,
            _assertion(
                run_id="r1",
                landing_mode="teleport",
                observer="some.future.observer",
                target_branch="experiment/not-a-release-line",
            ),
        )

        state = _states_by_run(tmp_path)["r1"]

        assert state["landing_state"] == "landed"
        assert state["landing_mode"] == "teleport"
        assert state["observer"] == "some.future.observer"
        assert state["target_branch"] == "experiment/not-a-release-line"

    def test_the_schema_constrains_none_of_those_columns(self) -> None:
        """No CHECK / enum in the DDL — an allow-list would filter the future out."""
        ddl = audit_storage._SCHEMA
        start = ddl.index("CREATE TABLE IF NOT EXISTS landing_assertions")
        end = ddl.index("CREATE INDEX IF NOT EXISTS idx_landing_assertions_slug")
        table = ddl[start:end]
        assert "CHECK" not in table.upper()
        for column in ("landing_mode", "target_branch", "observer"):
            assert f"    {column} TEXT," in table

    def test_an_unknown_value_is_not_mapped_onto_a_known_one(self, tmp_path: Path) -> None:
        """Two runs, two unknown modes: they stay two values, not one bucket."""
        _write_runs(tmp_path, [_record(run_id="r1"), _record(run_id="r2", slug="other")])
        landing_evidence.write_landing_assertion(
            tmp_path, _assertion(run_id="r1", landing_mode="mode-alpha")
        )
        landing_evidence.write_landing_assertion(
            tmp_path, _assertion(run_id="r2", slug="other", landing_mode="mode-beta")
        )

        states = _states_by_run(tmp_path)

        assert states["r1"]["landing_mode"] == "mode-alpha"
        assert states["r2"]["landing_mode"] == "mode-beta"


class TestRebuildParity:
    """AC3: from scratch and incrementally maintained answer identically."""

    def _corpus(self, project_root: Path) -> None:
        _write_runs(
            project_root,
            [_record(run_id=f"r{i}", slug=f"s{i}") for i in range(1, 5)],
        )
        landing_evidence.write_landing_assertion(project_root, _assertion(run_id="r1", slug="s1"))
        landing_evidence.write_landing_attempt(
            project_root, _attempt(run_id="r2", slug="s2", outcome="queued")
        )
        landing_evidence.write_landing_attempt(
            project_root, _attempt(run_id="r3", slug="s3", outcome="refused")
        )
        landing_evidence.write_landing_assertion(project_root, _assertion(run_id="r3", slug="s3"))

    def test_schema_version_was_bumped_for_the_projection(self) -> None:
        assert audit_storage.SUBSTRATE_SCHEMA_VERSION >= 13

    def test_rebuild_reconstructs_the_projection_from_the_artifacts(self, tmp_path: Path) -> None:
        self._corpus(tmp_path)

        incremental = _states_by_run(tmp_path)
        audit_storage.rebuild_from_runs(tmp_path)
        rebuilt = _states_by_run(tmp_path)

        assert rebuilt == incremental
        assert {run: row["landing_state"] for run, row in rebuilt.items()} == {
            "r1": "landed",
            "r2": "unresolved",
            "r3": "landed",
            "r4": "unresolved",
        }

    def test_evidence_written_after_indexing_refreshes_in_place(self, tmp_path: Path) -> None:
        """A landing observed later must reach the substrate on the next open."""
        _write_runs(tmp_path, [_record(run_id="r1")])
        assert _states_by_run(tmp_path)["r1"]["landing_state"] == "unresolved"

        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        assert _states_by_run(tmp_path)["r1"]["landing_state"] == "landed"

    def test_an_evidence_refresh_does_not_erase_non_reconstructible_tables(
        self, tmp_path: Path
    ) -> None:
        """The refresh is targeted, not a whole-database rebuild.

        ``rebuild_from_runs`` preserves only legacy rows and shape-skip events;
        readiness, shape-verdict, inline-remediation and triage rows have no
        file on disk to come back from. Landing observations arrive on every
        landed story, so routing them through the staleness rebuild would delete
        that history routinely. This is the regression guard for that.
        """
        _write_runs(tmp_path, [_record(run_id="r1")])
        # Index the run records first, so the only thing that changes below is
        # the evidence tree. (A *run record* appearing late still triggers the
        # pre-existing staleness rebuild; that is not what this test is about.)
        audit_storage.require_substrate(tmp_path).close()
        audit_storage.record_readiness_event(
            tmp_path,
            {
                "kind": "groom",
                "issue_ref": "#1",
                "action": "applied",
                "applied": True,
                "emitted_at": "2026-03-01T09:00:00+00:00",
            },
        )
        audit_storage.record_shape_verdict_event(
            tmp_path,
            {
                "issue_id": "1",
                "verdict": "READY",
                "emitted_at": "2026-03-01T09:00:00+00:00",
            },
        )
        audit_storage.record_inline_remediation_event(
            tmp_path,
            {
                "issue_id": "1",
                "action": "reshape",
                "succeeded": True,
                "emitted_at": "2026-03-01T09:00:00+00:00",
            },
        )

        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        conn = audit_storage.require_substrate(tmp_path)
        try:
            assert audit_read_model.landing_state_for_run(conn, "r1")["landing_state"] == "landed"
            for table in (
                "readiness_events",
                "shape_verdict_events",
                "inline_remediation_events",
            ):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert count == 1, f"{table} was erased by a landing-evidence refresh"
        finally:
            conn.close()

    def test_staging_evidence_is_projected_too(self, tmp_path: Path) -> None:
        """Evidence drained to the memory-staging area is still evidence."""
        _write_runs(tmp_path, [_record(run_id="r1")])
        staging = tmp_path.joinpath(*landing_evidence.PROJECT_MEMORY_STAGING_RELPATH).joinpath(
            *landing_evidence.LANDING_EVIDENCE_RELPATH
        )
        staging.mkdir(parents=True)
        (staging / "r1.landed.json").write_text(
            json.dumps(_assertion(run_id="r1")), encoding="utf-8"
        )

        assert _states_by_run(tmp_path)["r1"]["landing_state"] == "landed"


class TestReadOnlyOpeningContract:
    """A read-only opening answers from the last-indexed projection, and says so."""

    def test_status_reports_what_was_indexed_and_when(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))
        audit_storage.require_substrate(tmp_path).close()

        conn = audit_storage.open_readonly(tmp_path)
        try:
            status = audit_read_model.landing_projection_status(conn)
        finally:
            conn.close()

        assert status["assertions"] == 1
        assert status["attempts"] == 0
        assert status["source_artifacts"] == 1
        assert status["synced_at"] is not None

    def test_readonly_serves_the_last_index_and_the_status_shows_the_lag(
        self, tmp_path: Path
    ) -> None:
        """Evidence written after the last sync is absent — but not silently.

        ``open_readonly`` may not write, so it cannot re-index. The contract is
        that it answers from the last-indexed projection and that
        ``landing_projection_status`` exposes the artifact count it was built
        from, so a caller comparing that to the tree can tell "not re-indexed"
        from "never landed" and knows to run ``forge audits rebuild``.
        """
        _write_runs(tmp_path, [_record(run_id="r1")])
        audit_storage.require_substrate(tmp_path).close()
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        conn = audit_storage.open_readonly(tmp_path)
        try:
            state = audit_read_model.landing_state_for_run(conn, "r1")
            status = audit_read_model.landing_projection_status(conn)
        finally:
            conn.close()

        assert state["landing_state"] == "unresolved"
        assert status["source_artifacts"] == 0
        assert len(list(landing_evidence.landing_evidence_dir(tmp_path).glob("*.json"))) == 1

        # And a writable opening repairs it without operator intervention.
        assert _states_by_run(tmp_path)["r1"]["landing_state"] == "landed"


class TestFlattenedColumnReadersUnchanged:
    """AC5: follow-on 4's boundary. The flattened readers still read the column."""

    def test_latest_run_outcome_returns_the_stored_column_verbatim(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1", landing_status="not_landed")])

        conn = audit_storage.require_substrate(tmp_path)
        try:
            outcome = audit_read_model.latest_run_outcome_in_substrate(conn, "demo")
        finally:
            conn.close()

        assert outcome["landing_status"] == "not_landed"

    def test_latest_run_outcome_is_unmoved_when_the_projection_disagrees(
        self, tmp_path: Path
    ) -> None:
        """The case follow-on 4 exists for.

        The record says not landed (it was written before the pull request
        merged); the evidence says landed. The projection reports ``landed`` and
        this reader still reports the stored column. Both are true statements
        about different things, and the difference has to stay observable until
        the follow-on re-points this reader deliberately.
        """
        _write_runs(tmp_path, [_record(run_id="r1", landing_status="not_landed")])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        conn = audit_storage.require_substrate(tmp_path)
        try:
            outcome = audit_read_model.latest_run_outcome_in_substrate(conn, "demo")
            projected = audit_read_model.landing_state_for_run(conn, "r1")
        finally:
            conn.close()

        assert outcome["landing_status"] == "not_landed"
        assert projected["landing_state"] == "landed"

    def test_unset_flattened_column_stays_none(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_record(run_id="r1")])
        landing_evidence.write_landing_assertion(tmp_path, _assertion(run_id="r1"))

        conn = audit_storage.require_substrate(tmp_path)
        try:
            outcome = audit_read_model.latest_run_outcome_in_substrate(conn, "demo")
        finally:
            conn.close()

        assert outcome["landing_status"] is None


class TestFacadeExports:
    def test_projection_readers_are_reachable_through_the_facade(self) -> None:
        assert sub.landing_states is audit_read_model.landing_states
        assert sub.landing_state_for_run is audit_read_model.landing_state_for_run
        assert sub.landed_run_ids_in_substrate is audit_read_model.landed_run_ids_in_substrate
        assert sub.landing_projection_status is audit_read_model.landing_projection_status
        assert sub.sync_landing_evidence is audit_storage.sync_landing_evidence
