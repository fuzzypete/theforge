"""Seam tests for issue #2214: work must survive a re-exec generation boundary.

A sprint that re-execs begins a new generation of the same run. A story the
launch guard drops in the new generation may already have run dev, run review
and committed an implementation in the old one. The drop record is synthesized
from a state that never entered the state machine, so written as it stands it
says INIT, $0.00, unsuccessful — indistinguishable from a story that never
began, and every record derived from it inherits that silently.

These tests pin the accounting the drop leaves behind: the record reports the
phase the work reached and the budget it consumed, the sprint row and the sprint
total agree with it, and a generation that genuinely did nothing still records
nothing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from sprint_test_helpers import run_sprint_ctx

from tests.test_sprint_abnormal_evidence import _story_audits
from tests.test_sprint_launch_liveness import (
    _make_config,
    _make_coordinator_result,
    _make_manifest,
    _make_spec_file,
    _triage_full,
)
from theforge.coordinator import audit_substrate
from theforge.sprint.audit import (
    PriorGeneration,
    carry_prior_generation_work,
    load_prior_generation_story_audit,
    records_performed_work,
)
from theforge.sprint.dropped_work import WorktreeWork
from theforge.sprint.launch_guard import REASON_ACTIVE_WORKTREE, REASON_STRANDED_WORKTREE

SPRINT_NAME = "Test Sprint"
DROPPED_SLUG = "issue-2048"
PRIOR_RUN_ID = "priorgen0001"


# ── helpers ──────────────────────────────────────────────────────────


def _prior_generation_audit(
    *,
    run_id: str = PRIOR_RUN_ID,
    cost: float | None = 4.25,
    final_phase: str = "REVIEW",
    in_flight: bool = True,
) -> dict:
    """An audit shaped like one a generation flushed mid-flight (#2013)."""
    return {
        "run_id": run_id,
        "forge_version": "0.13.0",
        "in_flight": in_flight,
        "task": {"slug": DROPPED_SLUG, "name": "Issue 2048"},
        "outcome": {"success": False, "final_phase": final_phase, "message": "in flight"},
        "timing": {"started_at": "2026-08-05T03:00:00+00:00"},
        "workspace": {"path": f"/tmp/{DROPPED_SLUG}", "branch": f"feat/{DROPPED_SLUG}"},
        "iterations": {
            "dev_attempts_total": 2,
            "dev_iterations": 2,
            "review_cycles_total": 1,
            "gate_runs": 3,
            "gate_decisions": ["PASS"],
            "dev_loop": [{"iteration": 1}, {"iteration": 2}],
        },
        "cost": {
            "total_usd": cost,
            "dev_usd": 3.0,
            "review_usd": 1.25,
            "agents": [{"role": "dev", "model": "strong"}],
        },
        "reviews": [{"reviewer": "r1", "verdict": "APPROVE"}],
        "phases": {
            "preflight": {"cost_usd": 0.1, "outcome": "proceed"},
            "dev": {"cost_usd": 3.0, "outcome": "success"},
            "review": {"cost_usd": 1.25, "outcome": "approve"},
        },
        "totals": {"cost_usd": cost},
    }


def _write_prior_audit(tmp_path: Path, audit: dict, slug: str = DROPPED_SLUG) -> Path:
    log_dir = tmp_path / ".forge" / "logs" / SPRINT_NAME / slug
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "audit.yaml"
    path.write_text(yaml.dump(audit), encoding="utf-8")
    return path


def _run_sprint_with_drop(
    tmp_path: Path,
    *,
    reason: str = REASON_ACTIVE_WORKTREE,
    worktree_work: WorktreeWork | None = None,
):
    _make_spec_file(tmp_path, "Issue 2048", DROPPED_SLUG)
    _make_spec_file(tmp_path, "Issue 2060", "issue-2060")
    manifest_path = _make_manifest(tmp_path, [f"{DROPPED_SLUG}.md", "issue-2060.md"])
    config = _make_config(tmp_path)

    work_patch = (
        patch("theforge.sprint.runner.inspect_worktree_work", return_value=worktree_work)
        if worktree_work is not None
        else patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full)
    )
    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_gate,
        work_patch,
    ):
        mock_gate.return_value = {"passed": True, "message": "ok"}
        return run_sprint_ctx(
            config,
            manifest_path,
            run_id="run-2214",
            dropped_slugs={DROPPED_SLUG: reason},
        )


def _sprint_story_row(tmp_path: Path, slug: str = DROPPED_SLUG) -> dict:
    state_file = next((tmp_path / ".forge" / "sprints").glob("*/state.yaml"))
    stories = yaml.safe_load(state_file.read_text())["stories"]
    return {s["slug"]: s for s in stories}[slug]


# ── the drop record accounts for the work that ran ───────────────────


def test_drop_record_reports_the_phase_the_work_reached(tmp_path: Path) -> None:
    """A story that ran dev and review is not recorded as never having started."""
    _write_prior_audit(tmp_path, _prior_generation_audit())

    _run_sprint_with_drop(tmp_path)

    record = _story_audits(tmp_path)[DROPPED_SLUG]
    assert record["outcome"]["final_phase"] == "REVIEW", (
        "the drop record still reports INIT for a story that ran dev and review"
    )
    # The drop is still visible as what happened to *this* generation.
    assert record["outcome"]["dropped_at_phase"] == "INIT"
    assert record["abnormal_termination"]["kind"] == "launch_guard_drop"


def test_drop_record_carries_the_cost_the_work_consumed(tmp_path: Path) -> None:
    """Spend that produced committed output is never recorded as zero."""
    _write_prior_audit(tmp_path, _prior_generation_audit(cost=4.25))

    _run_sprint_with_drop(tmp_path)

    record = _story_audits(tmp_path)[DROPPED_SLUG]
    assert record["cost"]["total_usd"] == 4.25
    assert record["outcome"]["cost_usd"] == 4.25
    assert record["iterations"]["dev_attempts_total"] == 2
    assert record["reviews"], "the reviews the prior generation ran are absent"
    assert record["phases"]["dev"]["outcome"] == "success"


def test_drop_record_names_the_generation_the_work_came_from(tmp_path: Path) -> None:
    """Carried accounting is attributed, never presented as this generation's own."""
    _write_prior_audit(tmp_path, _prior_generation_audit())

    _run_sprint_with_drop(tmp_path)

    record = _story_audits(tmp_path)[DROPPED_SLUG]
    prior = record["prior_generation"]
    assert prior["run_id"] == PRIOR_RUN_ID
    assert prior["final_phase"] == "REVIEW"
    assert prior["cost_usd"] == 4.25
    assert prior["in_flight"] is True
    assert "cost" in prior["carried_keys"]
    # The record is linked to the run that produced the work, not orphaned from it.
    assert record["parent_run_id"] == PRIOR_RUN_ID
    assert record["run_id"] != PRIOR_RUN_ID


def test_indexed_record_is_no_longer_a_zero_cost_init_no_op(tmp_path: Path) -> None:
    """The substrate columns a reader queries carry the work, not the drop's INIT."""
    _write_prior_audit(tmp_path, _prior_generation_audit())

    _run_sprint_with_drop(tmp_path)

    conn = audit_substrate.require_substrate(tmp_path)
    try:
        row = conn.execute(
            "SELECT final_phase, total_cost_usd FROM audit_records WHERE slug = ?",
            (DROPPED_SLUG,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["final_phase"] == "REVIEW"
    assert row["total_cost_usd"] == 4.25


# ── the sprint's own surfaces agree with the record ──────────────────


def test_sprint_row_reports_the_recovered_cost(tmp_path: Path) -> None:
    """The summary row cannot say $0.00 while the record says $4.25."""
    _write_prior_audit(tmp_path, _prior_generation_audit())

    _run_sprint_with_drop(tmp_path)

    row = _sprint_story_row(tmp_path)
    assert row["outcome"] == "DROPPED"
    assert row["cost_usd"] == 4.25
    assert row["prior_generation_run_id"] == PRIOR_RUN_ID
    assert row["prior_generation_final_phase"] == "REVIEW"


def test_recovered_spend_reaches_the_sprint_total(tmp_path: Path) -> None:
    """Recovered spend is this sprint's spend; the total must include it."""
    _write_prior_audit(tmp_path, _prior_generation_audit())

    result = _run_sprint_with_drop(tmp_path)

    assert result.total_cost_usd >= 4.25, (
        "prior-generation spend was attributed to the story but not to the sprint"
    )
    assert result.cost_complete


def test_dropped_worktree_with_commits_reports_recovered_cost_not_unknown(
    tmp_path: Path,
) -> None:
    """A drop that abandons committed work reports what that work cost."""
    _write_prior_audit(tmp_path, _prior_generation_audit())
    work = WorktreeWork(
        slug=DROPPED_SLUG,
        path=str(tmp_path / DROPPED_SLUG),
        branch=f"feat/{DROPPED_SLUG}",
        exists=True,
        commits_ahead=1,
        dirty=False,
    )

    result = _run_sprint_with_drop(tmp_path, worktree_work=work)

    row = _sprint_story_row(tmp_path)
    assert row["cost_usd"] == 4.25
    assert not [s for s in result.unmeasured_spend_sources if DROPPED_SLUG in s], (
        "a cost recovered from the prior generation was still reported as unmeasured"
    )


def test_unmeasured_prior_spend_stays_unknown_never_zero(tmp_path: Path) -> None:
    """A prior generation whose spend was never measured is not recorded as free."""
    _write_prior_audit(tmp_path, _prior_generation_audit(cost=None))
    work = WorktreeWork(
        slug=DROPPED_SLUG,
        path=str(tmp_path / DROPPED_SLUG),
        branch=f"feat/{DROPPED_SLUG}",
        exists=True,
        commits_ahead=1,
        dirty=False,
    )

    result = _run_sprint_with_drop(tmp_path, worktree_work=work)

    row = _sprint_story_row(tmp_path)
    assert row["cost_usd"] is None
    assert any(DROPPED_SLUG in s for s in result.unmeasured_spend_sources)
    assert not result.cost_complete, "an unmeasured story certified a sprint total"


def test_stranded_drop_also_carries_the_prior_generation(tmp_path: Path) -> None:
    """The stranded-worktree drop is the same boundary and gets the same treatment."""
    _write_prior_audit(tmp_path, _prior_generation_audit())

    _run_sprint_with_drop(tmp_path, reason=REASON_STRANDED_WORKTREE)

    record = _story_audits(tmp_path)[DROPPED_SLUG]
    assert record["outcome"]["final_phase"] == "REVIEW"
    assert _sprint_story_row(tmp_path)["cost_usd"] == 4.25


# ── a generation that did nothing still records nothing ──────────────


def test_a_prior_generation_that_did_no_work_carries_nothing(tmp_path: Path) -> None:
    """The fix must not manufacture history for a story that really never ran."""
    _write_prior_audit(
        tmp_path,
        {
            "run_id": "emptygen0001",
            "in_flight": True,
            "outcome": {"success": False, "final_phase": "INIT"},
            "iterations": {
                "dev_attempts_total": 0,
                "review_cycles_total": 0,
                "gate_runs": 0,
                "usage_summary": {"dev": {"used": 0, "max": 3}},
            },
            "cost": {"total_usd": 0.0, "agents": []},
        },
    )

    _run_sprint_with_drop(tmp_path)

    record = _story_audits(tmp_path)[DROPPED_SLUG]
    assert "prior_generation" not in record
    assert record["outcome"]["final_phase"] == "INIT"
    assert _sprint_story_row(tmp_path)["cost_usd"] == 0.0


def test_no_prior_audit_leaves_the_drop_record_unchanged(tmp_path: Path) -> None:
    """With nothing flushed by an earlier generation there is nothing to carry."""
    _run_sprint_with_drop(tmp_path)

    record = _story_audits(tmp_path)[DROPPED_SLUG]
    assert "prior_generation" not in record
    assert record["outcome"]["final_phase"] == "INIT"


def test_independently_recorded_prior_spend_is_not_counted_twice(tmp_path: Path) -> None:
    """A prior generation with its own run record is linked, not restated.

    Its cost already stands as a record of its own; copying it into a second
    record would report the same dollars twice.
    """
    _write_prior_audit(tmp_path, _prior_generation_audit(in_flight=False))
    runs_dir = audit_substrate.runs_dir(tmp_path)
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{PRIOR_RUN_ID}.json").write_text(
        '{"run_id": "%s", "cost": {"total_usd": 4.25}}' % PRIOR_RUN_ID, encoding="utf-8"
    )

    result = _run_sprint_with_drop(tmp_path)

    record = _story_audits(tmp_path)[DROPPED_SLUG]
    assert record["prior_generation"]["independently_recorded"] is True
    assert record["parent_run_id"] == PRIOR_RUN_ID
    # The phase it reached is still reported — that is not a double count.
    assert record["outcome"]["final_phase"] == "REVIEW"
    assert record["cost"]["total_usd"] in (0.0, None)
    assert _sprint_story_row(tmp_path)["cost_usd"] == 0.0
    assert result.total_cost_usd < 4.25


# ── unit level: what counts as work, and what gets carried ───────────


def test_records_performed_work_ignores_configured_limits() -> None:
    """Zeroed counters beside a configured max are not evidence of work."""
    assert not records_performed_work(
        {"iterations": {"dev_attempts_total": 0, "usage_summary": {"dev": {"used": 0, "max": 3}}}}
    )
    assert records_performed_work({"iterations": {"dev_attempts_total": 1}})
    assert records_performed_work({"cost": {"total_usd": 0.5}})
    assert records_performed_work({"phases": {"dev": {"outcome": "success"}}})
    assert not records_performed_work({"phases": {"dev": None, "review": None}})
    assert not records_performed_work({})


def test_carry_leaves_this_generations_own_account_of_the_exit_intact() -> None:
    """The record still says how *this* generation ended, and why."""
    audit_data = {
        "run_id": "dropgen0001",
        "outcome": {
            "success": False,
            "final_phase": "INIT",
            "message": "Launch guard dropped issue-2048",
            "error_type": "LaunchGuardDrop",
        },
        "error": "Dropped before dispatch",
        "cost": {"total_usd": 0.0},
    }
    carried = carry_prior_generation_work(
        audit_data, PriorGeneration(audit=_prior_generation_audit())
    )

    assert "cost" in carried
    assert audit_data["run_id"] == "dropgen0001"
    assert audit_data["outcome"]["error_type"] == "LaunchGuardDrop"
    assert "Launch guard dropped" in audit_data["outcome"]["message"]
    assert audit_data["error"] == "Dropped before dispatch"
    assert audit_data["outcome"]["final_phase"] == "REVIEW"


def test_carry_never_overwrites_with_a_prior_generations_silence() -> None:
    """A section the prior generation left empty does not erase this record's."""
    audit_data = {
        "outcome": {"final_phase": "INIT"},
        "reviews": [{"reviewer": "current"}],
        "cost": {"total_usd": 0.0},
    }
    prior = _prior_generation_audit()
    prior["reviews"] = []

    carry_prior_generation_work(audit_data, PriorGeneration(audit=prior))

    assert audit_data["reviews"] == [{"reviewer": "current"}]


def test_loader_ignores_an_audit_from_this_generation(tmp_path: Path) -> None:
    """This generation's own flushed audit is not a prior generation's."""
    _write_prior_audit(tmp_path, _prior_generation_audit())

    assert (
        load_prior_generation_story_audit(
            tmp_path, SPRINT_NAME, DROPPED_SLUG, exclude_run_id=PRIOR_RUN_ID
        )
        is None
    )
    assert load_prior_generation_story_audit(tmp_path, SPRINT_NAME, DROPPED_SLUG) is not None


def test_loader_survives_an_unreadable_audit(tmp_path: Path) -> None:
    """Evidence recovery is best-effort and never raises into the drop path."""
    log_dir = tmp_path / ".forge" / "logs" / SPRINT_NAME / DROPPED_SLUG
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "audit.yaml").write_text("::: not : yaml :::\n  - [", encoding="utf-8")

    assert load_prior_generation_story_audit(tmp_path, SPRINT_NAME, DROPPED_SLUG) is None
    assert load_prior_generation_story_audit(tmp_path, None, DROPPED_SLUG) is None
