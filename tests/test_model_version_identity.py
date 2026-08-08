"""A model version is a routing candidate, and an alias records what served it.

Issue #2226. Before this, a routing candidate could be a model *family* rather
than a model: the catalog named unversioned vendor shorthands, the vendor
resolved them at invocation time, and the alias was what got recorded and what
evidence accumulated against. Two consequences, and this module pins both:

* evidence accrued to a moving target — observations of two different models
  were indistinguishable under one key;
* an earlier generation could not be *named*, so it could never be offered as a
  cheaper alternative.

The tests are grouped by the acceptance criterion they hold down.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from theforge.config.model_catalog import parse_definition, resolve_project
from theforge.config.models import AGENT_REGISTRY, resolve_agent_spec
from theforge.coordinator import audit_substrate as sub
from theforge.coordinator.agent_identity import invocation_identity_rows
from theforge.model_profiles import (
    RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY,
    RESOLVED_MODEL_BREAKDOWN_KEY,
    ReviewerAttempt,
    RoleAttempt,
    RunOutcome,
    apply_run,
    canonical_id_for_legacy_key,
    get_dev_signal,
    get_review_signal,
    get_role_reliability_signal,
)

ALIAS = "anthropic/opus/cli"
PINNED = "anthropic/claude-opus-4-6/cli"
CURRENT = "anthropic/claude-opus-5/cli"


# ── AC1: a version is expressible as a candidate beside the alias ────────


class TestVersionIsExpressibleAsACandidate:
    def test_alias_and_pinned_versions_are_separate_registry_identities(self) -> None:
        """Both are in the catalog, under different canonical ids, at once."""
        assert ALIAS in AGENT_REGISTRY
        assert PINNED in AGENT_REGISTRY
        assert CURRENT in AGENT_REGISTRY
        assert AGENT_REGISTRY[ALIAS].model == "opus"
        assert AGENT_REGISTRY[PINNED].model == "claude-opus-4-6"

    def test_every_shorthand_family_has_a_pinned_counterpart(self) -> None:
        anthropic_cli = {
            spec.model
            for key, spec in AGENT_REGISTRY.items()
            if spec.provider == "anthropic" and spec.transport.kind == "cli"
        }
        # Shorthands kept; concrete versions added beside them, not instead.
        assert {"opus", "sonnet", "haiku"} <= anthropic_cli
        assert {"claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"} <= anthropic_cli

    def test_a_pinned_version_resolves_through_the_ordinary_lookup(self) -> None:
        """It is nameable exactly the way any other model is — no special case."""
        spec = resolve_agent_spec(PINNED)
        assert (spec.provider, spec.model, spec.transport.kind) == (
            "anthropic",
            "claude-opus-4-6",
            "cli",
        )

    def test_an_earlier_generation_is_banded_from_its_own_attributable_price(self) -> None:
        """The cost-vs-capability tradeoff across generations becomes expressible.

        The shorthand bands on the vendor's tier naming (it has no attributable
        price — it can move). A pinned version is a billed identity, so its band
        is derived from its own price and the two can legitimately disagree.
        """
        pinned = AGENT_REGISTRY[PINNED]
        assert pinned.pricing_provenance == "claude-opus-4-6"
        assert pinned.routing.cost_rank_basis == "price:claude-opus-4-6"
        assert pinned.effective_input_cost_per_mtok == 15.00

        alias = AGENT_REGISTRY[ALIAS]
        assert alias.pricing_provenance is None
        assert alias.routing.cost_rank_basis == "vendor-tier"
        # The alias's indicative literal is invisible to routing.
        assert alias.effective_input_cost_per_mtok is None

    def test_a_project_can_declare_its_own_pin_beside_the_shipped_shorthand(self) -> None:
        """models.custom can name a version the catalog has not pinned."""
        definition = parse_definition(
            {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "transport": {"kind": "cli"},
                "routing": {"tier": "strong", "capability": 9, "cost_rank": 2},
                "cost": {
                    "input_per_mtok": 5.00,
                    "output_per_mtok": 25.00,
                    "pricing_provenance": "claude-opus-4-8",
                },
            },
            where="models.custom[0]",
        )
        resolved = resolve_project(
            definition,
            where="models.custom[0]",
            builtin=AGENT_REGISTRY.get(definition.canonical_id),
        )
        assert resolved.canonical_id == "anthropic/claude-opus-4-8/cli"
        assert resolved.spec.model == "claude-opus-4-8"
        # The shipped shorthand is untouched by the project's pin.
        assert AGENT_REGISTRY[ALIAS].model == "opus"


# ── AC5: an alias-only configuration keeps working ───────────────────────


class TestAliasOnlyConfigurationKeepsWorking:
    def test_shorthands_still_resolve_and_still_carry_no_version(self) -> None:
        for shorthand in ("anthropic/opus/cli", "anthropic/sonnet/cli", "anthropic/haiku/cli"):
            spec = resolve_agent_spec(shorthand)
            assert spec.transport.kind == "cli"
            # Nothing forced a version onto it: it still means "whatever ships".
            assert "-" not in spec.model

    def test_legacy_alias_spellings_still_normalize_to_the_shorthand(self) -> None:
        assert canonical_id_for_legacy_key("claude/opus") == ALIAS
        assert canonical_id_for_legacy_key("opus") == ALIAS
        assert canonical_id_for_legacy_key("sonnet-cli") == "anthropic/sonnet/cli"


# ── AC2: what served is recorded, distinguishably from what selected ─────


def _ledger(configured: str, resolved: str, *, role: str = "dev") -> dict:
    """A minimal recorded invocation ledger, as ``audit_render`` writes it."""
    from theforge.coordinator.agent_identity import canonicalize_identity

    def _block(raw: str) -> dict:
        identity, resolution = canonicalize_identity(raw, {"transport_used": "cli"})
        return {
            "raw": raw,
            "transport": "cli",
            "identity": identity,
            "resolution": resolution,
        }

    configured_block, resolved_block = _block(configured), _block(resolved)
    return {
        "version": 1,
        "role": role,
        "profile": role,
        "configured_identity": configured_block,
        "resolved_primary_identity": resolved_block,
        "configured_differs_from_resolved": (
            configured_block["identity"] != resolved_block["identity"]
        ),
        "billed_components": [],
    }


def _record(run_id: str, *, started_at: str, served: str, configured: str = "opus") -> dict:
    return {
        "run_id": run_id,
        "task": {"slug": "demo", "name": "demo"},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": started_at, "duration_seconds": 60.0},
        "totals": {"cost_usd": 1.0, "duration_s": 60.0},
        "cost": {
            "total_usd": 1.0,
            "agents": [
                {
                    "role": "dev",
                    "profile": "dev",
                    "cost_usd": 1.0,
                    "ledger": _ledger(configured, served),
                }
            ],
        },
    }


class TestServedVersionIsRecordedDistinguishably:
    def test_configured_alias_and_resolved_version_are_separate_columns(
        self, tmp_path: Path
    ) -> None:
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(
                conn,
                _record("r1", started_at="2026-03-01T10:00:00+00:00", served="claude-opus-4-6"),
                provenance="native",
            )
            conn.commit()
            row = conn.execute(
                "SELECT dev_configured_model, dev_resolved_model FROM audit_records "
                "WHERE run_id = 'r1'"
            ).fetchone()
        finally:
            conn.close()
        assert tuple(row) == (ALIAS, PINNED)

    def test_every_recorded_invocation_gets_an_identity_row_not_just_dev(self) -> None:
        """Role-neutral: the dev-only projection cannot answer for other phases."""
        record = {
            "cost": {
                "agents": [
                    {
                        "role": "preflight",
                        "ledger": _ledger("haiku", "claude-haiku-4-5", role="preflight"),
                    },
                    {"role": "plan", "ledger": _ledger("opus", "claude-opus-5", role="plan")},
                    {"role": "dev", "ledger": _ledger("opus", "claude-opus-4-6")},
                    {
                        "role": "review",
                        "ledger": _ledger("sonnet", "claude-sonnet-4-6", role="review"),
                    },
                ]
            }
        }
        rows = invocation_identity_rows(record)
        assert [r["role"] for r in rows] == ["preflight", "plan", "dev", "review"]
        assert [r["resolved"][0] for r in rows] == [
            "anthropic/claude-haiku-4-5/cli",
            CURRENT,
            PINNED,
            "anthropic/claude-sonnet-4-6/cli",
        ]
        assert all(row["differs"] for row in rows)

    def test_the_final_preflight_attempt_is_indexed_once_not_twice(self) -> None:
        """The two record surfaces overlap by exactly one entry (#2226).

        ``preflight.attempts`` records EVERY attempt including the final one, and
        that final attempt is what becomes ``state.preflight_result`` and hence
        the ``cost.agents`` preflight entry. A normal preflight — one attempt, no
        retry — must therefore still produce one identity row, or alias-drift
        counts every ordinary run's preflight twice.
        """
        final = _ledger("opus", "claude-opus-5", role="preflight")
        record = {
            "cost": {"agents": [{"role": "preflight", "profile": "preflight", "ledger": final}]},
            "preflight": {"attempts": [{"profile_name": "preflight", "ledger": final}]},
        }
        rows = invocation_identity_rows(record)
        assert len(rows) == 1
        assert rows[0]["source"] == "cost.agents"
        assert rows[0]["resolved"][0] == CURRENT

    def test_a_superseded_preflight_attempt_is_indexed_beside_the_final_one(self) -> None:
        """The superseded attempts exist nowhere but ``preflight.attempts``.

        A fallback run records two attempts: the failed primary and the fallback
        that replaced it. Only the fallback reaches ``cost.agents``, so dropping
        ``preflight.attempts`` entirely would lose exactly the retry path where
        the served identity is most likely to differ from the configured one —
        while keeping all of it double-counts the final attempt.
        """
        superseded = _ledger("sonnet", "claude-sonnet-4-6", role="preflight")
        final = _ledger("opus", "claude-opus-5", role="preflight")
        record = {
            "cost": {"agents": [{"role": "preflight", "profile": "preflight", "ledger": final}]},
            "preflight": {
                "attempts": [
                    {"profile_name": "preflight", "ledger": superseded},
                    {"profile_name": "preflight", "ledger": final},
                ]
            },
        }
        rows = invocation_identity_rows(record)
        assert [r["source"] for r in rows] == ["cost.agents", "preflight.attempts"]
        assert {r["resolved"][0] for r in rows} == {
            CURRENT,
            "anthropic/claude-sonnet-4-6/cli",
        }

    def test_a_same_profile_parse_retry_is_not_dropped_as_a_duplicate(self) -> None:
        """Identical ledgers are expected on a parse retry — de-dup is positional.

        A same-profile parse retry resolves to the same version as the attempt it
        replaced, so its ledger signature matches. Dropping on a signature match
        alone would erase a real invocation; only the LAST attempt can be the one
        that also reached ``cost.agents``.
        """
        same = _ledger("opus", "claude-opus-5", role="preflight")
        record = {
            "cost": {"agents": [{"role": "preflight", "profile": "preflight", "ledger": same}]},
            "preflight": {
                "attempts": [
                    {"profile_name": "preflight", "ledger": same},
                    {"profile_name": "preflight", "ledger": same},
                ]
            },
        }
        rows = invocation_identity_rows(record)
        # Two invocations happened: the retried one and the one that survived.
        assert len(rows) == 2
        assert [r["source"] for r in rows] == ["cost.agents", "preflight.attempts"]

    def test_a_final_attempt_absent_from_cost_agents_is_still_indexed(self) -> None:
        """Nothing to duplicate → nothing to skip.

        ``audit_render`` defensively omits a preflight entry whose result is not a
        readable AgentResult. The attempt must not vanish because of a positional
        rule that assumed the duplicate was there.
        """
        record = {
            "cost": {"agents": []},
            "preflight": {
                "attempts": [
                    {
                        "profile_name": "preflight",
                        "ledger": _ledger("opus", "claude-opus-5", role="preflight"),
                    }
                ]
            },
        }
        rows = invocation_identity_rows(record)
        assert [r["source"] for r in rows] == ["preflight.attempts"]

    def test_a_pre_ledger_record_still_indexes_and_says_it_is_partial(self) -> None:
        rows = invocation_identity_rows(
            {"cost": {"agents": [{"role": "dev", "model_used": "claude-opus-4-6"}]}}
        )
        assert len(rows) == 1
        assert rows[0]["full_ledger"] is False
        assert rows[0]["configured"] is None  # never recorded — not guessed at
        assert rows[0]["resolved"][0] == PINNED


# ── AC4: a change in what an alias resolves to is detectable ─────────────


class TestAliasResolutionChangeIsDetectable:
    def _indexed(self, tmp_path: Path, records: list[dict]) -> sqlite3.Connection:
        conn = sub.create_or_open(tmp_path)
        for record in records:
            sub.upsert_run_record(conn, record, provenance="native")
        conn.commit()
        return conn

    def test_two_runs_under_one_alias_with_different_versions_report_a_change(
        self, tmp_path: Path
    ) -> None:
        conn = self._indexed(
            tmp_path,
            [
                _record("r1", started_at="2026-03-01T10:00:00+00:00", served="claude-opus-4-6"),
                _record("r2", started_at="2026-04-01T10:00:00+00:00", served="claude-opus-5"),
            ],
        )
        try:
            timeline = sub.alias_resolution_timeline(conn)
        finally:
            conn.close()

        assert len(timeline) == 1
        entry = timeline[0]
        assert entry["configured_model"] == ALIAS
        assert entry["changed"] is True
        assert entry["distinct_resolved"] == 2
        assert [r["resolved_model"] for r in entry["resolved_models"]] == [PINNED, CURRENT]
        assert entry["current"] == CURRENT
        assert entry["resolved_models"][0]["first_run_id"] == "r1"
        assert entry["resolved_models"][1]["first_seen"] == "2026-04-01T10:00:00+00:00"

    def test_a_stable_alias_is_not_reported_as_changed(self, tmp_path: Path) -> None:
        conn = self._indexed(
            tmp_path,
            [
                _record("r1", started_at="2026-03-01T10:00:00+00:00", served="claude-opus-4-6"),
                _record("r2", started_at="2026-04-01T10:00:00+00:00", served="claude-opus-4-6"),
            ],
        )
        try:
            timeline = sub.alias_resolution_timeline(conn)
        finally:
            conn.close()
        assert timeline[0]["changed"] is False
        assert timeline[0]["resolved_models"][0]["invocations"] == 2

    def test_reupserting_a_run_replaces_its_rows_rather_than_accumulating(
        self, tmp_path: Path
    ) -> None:
        record = _record("r1", started_at="2026-03-01T10:00:00+00:00", served="claude-opus-4-6")
        conn = self._indexed(tmp_path, [record, record])
        try:
            count = conn.execute("SELECT COUNT(*) FROM invocation_identities").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_opening_an_older_substrate_backfills_the_index_from_raw_json(
        self, tmp_path: Path
    ) -> None:
        """The new query surface has to reach already-indexed history."""
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(
                conn,
                _record("r1", started_at="2026-03-01T10:00:00+00:00", served="claude-opus-5"),
                provenance="native",
            )
            conn.execute("DELETE FROM invocation_identities")
            conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
            conn.commit()
        finally:
            conn.close()

        conn = sub.create_or_open(tmp_path)
        try:
            timeline = sub.alias_resolution_timeline(conn)
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert version == str(sub.SUBSTRATE_SCHEMA_VERSION)
        assert timeline[0]["resolved_models"][0]["resolved_model"] == CURRENT


# ── AC3: an alias's evidence is attributable to the versions ─────────────


def _dev_run(*, served: str | None, success: bool = True) -> RunOutcome:
    return RunOutcome(
        complexity="medium",
        dev_model=ALIAS,
        dev_success=success,
        dev_iterations=1,
        dev_cost_usd=1.0,
        dev_resolved_model=served,
        domains=["api"],
    )


class TestAliasEvidenceIsAttributableToVersions:
    def test_a_population_spanning_two_versions_says_so(self) -> None:
        data: dict = {"models": {}}
        for _ in range(3):
            apply_run(data, _dev_run(served=PINNED))
        for _ in range(2):
            apply_run(data, _dev_run(served=CURRENT, success=False))

        dev = data["models"][ALIAS]["dev"]
        assert dev["runs"] == 5
        assert dev[RESOLVED_MODEL_BREAKDOWN_KEY][PINNED]["runs"] == 3
        assert dev[RESOLVED_MODEL_BREAKDOWN_KEY][CURRENT]["runs"] == 2

        signal = get_dev_signal(data, ALIAS, min_runs=3)
        population = signal["resolved_population"]
        assert population["mixed"] is True
        assert population["distinct"] == 2
        assert population["by_resolved_model"] == {PINNED: 3, CURRENT: 2}
        assert population["attributed"] == 5
        # The rate itself is untouched — this is additive explainability.
        assert signal["runs"] == 5
        assert signal["rate"] == pytest.approx(0.6, abs=0.05)

    def test_a_single_version_population_is_reported_as_not_mixed(self) -> None:
        data: dict = {"models": {}}
        for _ in range(3):
            apply_run(data, _dev_run(served=PINNED))
        population = get_dev_signal(data, ALIAS, min_runs=3)["resolved_population"]
        assert population["mixed"] is False
        assert population["distinct"] == 1

    def test_history_with_no_recorded_version_is_unattributed_not_single_model(self) -> None:
        """A pre-#2226 population must not read as "one model" by omission."""
        data: dict = {"models": {}}
        for _ in range(3):
            apply_run(data, _dev_run(served=None))
        assert get_dev_signal(data, ALIAS, min_runs=3)["resolved_population"] == {}

        # Mixed old-and-new history reports the coverage gap explicitly.
        apply_run(data, _dev_run(served=PINNED))
        population = get_dev_signal(data, ALIAS, min_runs=3)["resolved_population"]
        assert population["attributed"] == 1
        assert population["unattributed"] == 3

    def test_the_per_complexity_band_carries_its_own_version_breakdown(self) -> None:
        data: dict = {"models": {}}
        for _ in range(3):
            apply_run(data, _dev_run(served=PINNED))
        band = get_dev_signal(data, ALIAS, complexity="medium", min_runs=3)
        assert band["resolved_population"]["by_resolved_model"] == {PINNED: 3}

    def test_reviewer_and_role_signals_report_their_version_population(self) -> None:
        data: dict = {"models": {}}
        outcome = RunOutcome(
            complexity="medium",
            dev_model=ALIAS,
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            preflight_attempts=[
                RoleAttempt(name=ALIAS, completed=True, resolved_model=PINNED),
                RoleAttempt(name=ALIAS, completed=True, resolved_model=CURRENT),
            ],
            reviewer_attempts=[
                ReviewerAttempt(
                    name="anthropic/sonnet/cli",
                    completed_parseable_verdict=True,
                    resolved_model="anthropic/claude-sonnet-4-6/cli",
                )
            ],
        )
        apply_run(data, outcome)

        preflight = get_role_reliability_signal(data, ALIAS, "preflight", min_runs=1)
        assert preflight["resolved_population"]["mixed"] is True

        review = get_review_signal(data, "anthropic/sonnet/cli", min_runs=1)
        assert review["resolved_population"]["by_resolved_model"] == {
            "anthropic/claude-sonnet-4-6/cli": 1
        }

    def test_phase_and_attempt_telemetry_for_one_run_do_not_double_count(self) -> None:
        """A version breakdown must sum to the counter it explains.

        preflight/planner/review sections carry TWO populations folded from the
        same run at different call sites: ``runs`` (phase cost/cycles) and
        ``_attempted_count`` (per-invocation completion). A single shared
        breakdown would be incremented by both and report more attributed
        version observations than either counter holds — the signal would claim
        more evidence about a version than its own rate is computed over.
        """
        data: dict = {"models": {}}
        outcome = RunOutcome(
            complexity="medium",
            dev_model=ALIAS,
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            # Phase-level preflight telemetry AND the matching attempt record.
            preflight_model=ALIAS,
            preflight_actual_model="opus",
            preflight_cli="claude",
            preflight_cost_usd=0.1,
            preflight_resolved_model=PINNED,
            preflight_attempts=[RoleAttempt(name=ALIAS, completed=True, resolved_model=PINNED)],
            # Same for the planner.
            planner_model=ALIAS,
            planner_actual_model="opus",
            planner_cli="claude",
            planner_cost_usd=0.2,
            planner_resolved_model=PINNED,
            planner_attempts=[RoleAttempt(name=ALIAS, completed=True, resolved_model=PINNED)],
            # And for the reviewer: findings/cost telemetry plus a completion attempt.
            reviewers={"anthropic/sonnet/cli": (1, 2, 0.3)},
            reviewer_attempts=[
                ReviewerAttempt(
                    name="anthropic/sonnet/cli",
                    completed_parseable_verdict=True,
                    actual_model="sonnet",
                    cli="claude",
                    resolved_model="anthropic/claude-sonnet-4-6/cli",
                )
            ],
        )
        apply_run(data, outcome)

        for role in ("preflight", "planner"):
            signal = get_role_reliability_signal(data, ALIAS, role, min_runs=1)
            population = signal["resolved_population"]
            # One attempt was folded, so exactly one attributed observation.
            assert population["by_resolved_model"] == {PINNED: 1}
            assert population["attributed"] == signal["attempted"] == 1
            assert population["unattributed"] == 0
            # The phase counter keeps its own, separately-scoped breakdown.
            section = data["models"][ALIAS][role]
            assert section[RESOLVED_MODEL_BREAKDOWN_KEY][PINNED]["runs"] == 1
            assert section[RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY][PINNED]["runs"] == 1

        review = get_review_signal(data, "anthropic/sonnet/cli", min_runs=1)
        population = review["resolved_population"]
        assert population["by_resolved_model"] == {"anthropic/claude-sonnet-4-6/cli": 1}
        assert population["attributed"] == review["attempted"] == 1
        assert population["unattributed"] == 0

    def test_review_cycles_served_by_different_versions_split_the_breakdown(self) -> None:
        """``review.runs`` counts CYCLES, and cycles can be served by two models.

        ``reviewers`` aggregates findings/cost across every cycle a reviewer took
        part in. Attributing that whole aggregate to one served version would be
        a false claim about which model produced the evidence — the story's own
        defect, one level down. The breakdown is therefore a per-version CYCLE
        COUNT, joined per (reviewer, cycle).
        """
        data: dict = {"models": {}}
        old = "anthropic/claude-sonnet-4-6/cli"
        new = "anthropic/claude-sonnet-5/cli"
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model=ALIAS,
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=1.0,
                reviewers={"anthropic/sonnet/cli": (3, 6, 0.9)},
                reviewer_resolved_cycles={"anthropic/sonnet/cli": {old: 2, new: 1}},
                reviewer_attempts=[
                    ReviewerAttempt(
                        name="anthropic/sonnet/cli",
                        completed_parseable_verdict=True,
                        actual_model="sonnet",
                        cli="claude",
                        resolved_model=new,
                    )
                ],
            ),
        )
        rev = data["models"]["anthropic/sonnet/cli"]["review"]
        assert rev["runs"] == 3
        # Sums to the counter it explains, split by the version per cycle.
        assert rev[RESOLVED_MODEL_BREAKDOWN_KEY][old]["runs"] == 2
        assert rev[RESOLVED_MODEL_BREAKDOWN_KEY][new]["runs"] == 1
        # ...while the attempt breakdown tracks the single recorded invocation.
        assert rev["_attempted_count"] == 1
        assert rev[RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY][new]["runs"] == 1
        assert old not in rev[RESOLVED_MODEL_ATTEMPT_BREAKDOWN_KEY]

    def test_a_cycle_with_no_recorded_version_under_claims_rather_than_guesses(self) -> None:
        """The breakdown never sums past the cycle count it explains."""
        data: dict = {"models": {}}
        served = "anthropic/claude-sonnet-4-6/cli"
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model=ALIAS,
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=1.0,
                reviewers={"anthropic/sonnet/cli": (3, 6, 0.9)},
                # Only one of the three cycles recorded a served identity.
                reviewer_resolved_cycles={"anthropic/sonnet/cli": {served: 1}},
            ),
        )
        rev = data["models"]["anthropic/sonnet/cli"]["review"]
        assert rev["runs"] == 3
        assert sum(b["runs"] for b in rev[RESOLVED_MODEL_BREAKDOWN_KEY].values()) == 1

    def test_a_tainted_run_is_tallied_per_version_and_still_excluded(self) -> None:
        data: dict = {"models": {}}
        outcome = _dev_run(served=PINNED)
        outcome.dev_tainted = True
        apply_run(data, outcome)
        dev = data["models"][ALIAS]["dev"]
        assert dev.get("runs", 0) == 0
        assert dev["tainted_runs"] == 1
        assert dev[RESOLVED_MODEL_BREAKDOWN_KEY][PINNED]["tainted_runs"] == 1


class TestAliasDerivedEvidenceOnTheConcreteCandidate:
    def test_an_alias_served_run_gives_the_pinned_candidate_evidence(self) -> None:
        data: dict = {"models": {}}
        for _ in range(3):
            apply_run(data, _dev_run(served=PINNED))

        derived = data["models"][PINNED]["alias_derived"]["dev"]
        assert derived["runs"] == 3
        assert derived["by_configured_model"] == {ALIAS: {"runs": 3, "_successes": 3.0}}

    def test_the_projection_never_inflates_the_concrete_candidates_own_counts(self) -> None:
        """One run, counted once wherever counts are summed across candidates."""
        data: dict = {"models": {}}
        apply_run(data, _dev_run(served=PINNED))

        assert data["models"][ALIAS]["dev"]["runs"] == 1
        # The concrete entry has NO dev section — its own history is still empty.
        assert "dev" not in data["models"][PINNED]
        assert get_dev_signal(data, PINNED, min_runs=1)["runs"] == 0

        total = sum(entry.get("dev", {}).get("runs", 0) for entry in data["models"].values())
        assert total == 1

    def test_a_pinned_candidate_that_ran_under_its_own_name_projects_nothing(self) -> None:
        """No second candidate is involved, so there is nothing to attribute."""
        data: dict = {"models": {}}
        outcome = RunOutcome(
            complexity="medium",
            dev_model=PINNED,
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            dev_resolved_model=PINNED,
        )
        apply_run(data, outcome)
        assert "alias_derived" not in data["models"][PINNED]
        assert data["models"][PINNED]["dev"]["runs"] == 1
