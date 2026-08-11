"""A recorded outcome survives an unmeasured cost across every surface (#2373).

A story that ran preflight/plan/plan-review, was approved there, and was then
skipped because the story it had been made to depend on failed, reached the
operator as *failed*, classified ``unknown_needs_rca``, with a recommendation to
buy an LLM diagnosis. Three surfaces described it three ways: the per-story audit
recorded a successful PLAN_REVIEW, the summary row recorded a skip whose reason
never reached it, and the operator digest classified what was left.

These tests hold the seam end to end — summary writer → RCA → digest / status —
so the sprint's recorded outcome stays the story's outcome, an unmeasured cost is
reported as accounting rather than as a result, and a disagreement between the
surfaces is reported instead of silently resolved to the worst-looking one.
"""

from __future__ import annotations

import datetime
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from theforge.sprint.rca import build_sprint_rca

_BLOCKED_STORY = "issue-2372"
_SKIP_REASON = f"dependency failed: {_BLOCKED_STORY}"


# ── Fixture builders ─────────────────────────────────────────────────────────


def _write_story_audit(sprint_log_dir: Path, slug: str) -> None:
    """The audit half of the disagreement: a successful PLAN_REVIEW, no cost."""
    story_dir = sprint_log_dir / slug
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "audit.yaml").write_text(
        yaml.safe_dump(
            {
                "story": {"slug": slug, "path": f"Issue #{slug.rsplit('-', 1)[-1]}"},
                "outcome": {
                    "final_phase": "PLAN_REVIEW",
                    "success": True,
                    "error": None,
                    "error_type": None,
                },
                "cost": {"total_cost_usd": None},
            }
        ),
        encoding="utf-8",
    )


def _write_summary(
    tmp_path: Path,
    *,
    error: str | None,
    slug: str = "issue-2373",
    with_audit: bool = True,
) -> Path:
    """Summary carrying the approved-then-skipped story with unmeasured cost."""
    sprint_log_dir = tmp_path / ".forge" / "logs" / "v0.13"
    sprint_log_dir.mkdir(parents=True, exist_ok=True)
    if with_audit:
        _write_story_audit(sprint_log_dir, slug)
    summary = {
        "sprint": {
            "name": "v0.13",
            "run_id": "run-2373",
            "finished_at": "2026-08-11T03:00:00Z",
            "total_cost_usd": None,
            "duration_seconds": 600.0,
        },
        "stories": [
            {
                "slug": slug,
                "path": f"Issue #{slug.rsplit('-', 1)[-1]}",
                "outcome": "SKIPPED",
                "outcome_code": "skipped",
                "verdict": "APPROVE",
                # Unmeasured — the accounting gap that used to consume the outcome.
                "cost_usd": None,
                "status": "cost_unknown",
                "error": error,
                "error_type": None,
                # The blocking edge was injected by the scheduler after a file
                # collision, so the row's declared dependency list is empty.
                "depends_on": [],
                "started_at": "2026-08-11T02:00:00Z",
                "finished_at": "2026-08-11T02:10:00Z",
            },
            {
                "slug": _BLOCKED_STORY,
                "path": "Issue #2372",
                "outcome": "ESCALATE",
                "outcome_code": "escalate",
                "cost_usd": 3.0,
                "error": "review requested changes",
            },
        ],
    }
    summary_path = sprint_log_dir / "sprint-summary.yaml"
    summary_path.write_text(yaml.safe_dump(summary), encoding="utf-8")
    return summary_path


def _entry(summary_path: Path, slug: str = "issue-2373") -> dict:
    payload = build_sprint_rca(summary_path)
    assert payload is not None
    return payload["stories"][slug]


def _actions_text(entry: dict) -> str:
    return " ".join(entry["recommended_next_actions"]).lower()


# ── RCA classification ───────────────────────────────────────────────────────


def test_dependency_skip_with_unmeasured_cost_stays_a_dependency_skip(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=_SKIP_REASON)
    entry = _entry(summary_path)

    assert entry["primary_failure_class"] == "dependency_skip"
    assert "forge diagnose" not in _actions_text(entry)


def test_skip_with_no_recorded_reason_is_not_unknown_needs_rca(tmp_path: Path) -> None:
    """The end state is recorded; only the reason is missing.

    ``unknown_needs_rca`` carries a recommendation to buy an investigation, and
    attaching it here spends money to establish that a skip was a skip.
    """
    summary_path = _write_summary(tmp_path, error=None)
    entry = _entry(summary_path)

    assert entry["primary_failure_class"] == "skip_reason_unrecorded"
    assert "forge diagnose" not in _actions_text(entry)
    assert "sprint log" in _actions_text(entry)


def test_unmeasured_cost_is_reported_as_accounting_beside_the_outcome(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=_SKIP_REASON)
    entry = _entry(summary_path)

    assert entry["cost_accounting"] == {"measured": False, "status": "cost_unknown"}
    # The accounting condition is reported; it does not become the class.
    assert entry["primary_failure_class"] == "dependency_skip"


def test_measured_cost_reports_measured_accounting(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=_SKIP_REASON)
    summary = yaml.safe_load(summary_path.read_text())
    summary["stories"][0]["cost_usd"] = 0.47
    summary["stories"][0].pop("status")
    summary_path.write_text(yaml.safe_dump(summary), encoding="utf-8")

    entry = _entry(summary_path)
    assert entry["cost_accounting"] == {"measured": True}
    assert entry["primary_failure_class"] == "dependency_skip"


# ── Cross-surface disagreement ───────────────────────────────────────────────


def test_audit_summary_disagreement_is_reported(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=_SKIP_REASON)
    entry = _entry(summary_path)

    consistency = entry["outcome_consistency"]
    assert consistency["agrees"] is False
    assert consistency["summary_outcome"] == "SKIPPED"
    assert consistency["audit_final_phase"] == "PLAN_REVIEW"
    assert consistency["audit_success"] is True
    assert consistency["authoritative"] == "summary_outcome"


def test_no_disagreement_reported_when_surfaces_agree(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=_SKIP_REASON, with_audit=False)
    sprint_log_dir = summary_path.parent
    story_dir = sprint_log_dir / "issue-2373"
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "audit.yaml").write_text(
        yaml.safe_dump(
            {
                "story": {"slug": "issue-2373"},
                "outcome": {"final_phase": "SKIPPED", "success": False},
            }
        ),
        encoding="utf-8",
    )

    assert "outcome_consistency" not in _entry(summary_path)


# ── Operator-facing surfaces ─────────────────────────────────────────────────


def test_status_reports_the_story_as_skipped(tmp_path: Path) -> None:
    from theforge.sprint.status_reader import read_completed_status

    summary_path = _write_summary(tmp_path, error=_SKIP_REASON)
    entries = {e.slug: e for e in read_completed_status(summary_path)}

    assert entries["issue-2373"].status == "skipped"
    # Unmeasured, not free.
    assert entries["issue-2373"].cost_usd is None


def _render_digest(tmp_path: Path, run_id: str = "run-2373") -> str:
    from theforge.cli.sprint_digest import display_sprint_digest

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        rc = display_sprint_digest(run_id, tmp_path)
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


def _persist_rca(summary_path: Path) -> None:
    from theforge.sprint.rca import write_sprint_rca

    write_sprint_rca(summary_path.parent, summary_path=summary_path)


def test_digest_renders_the_skip_under_skipped_not_failed(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=_SKIP_REASON)
    _persist_rca(summary_path)
    out = _render_digest(tmp_path)

    skipped_section = out.split("SKIPPED / INTAKE", 1)
    assert len(skipped_section) == 2, out
    assert "#2373" in skipped_section[1]
    # Every FAILED heading in the digest belongs to the blocking story, not to
    # the story that was skipped because of it.
    failed_block = out.split("FAILED — ", 1)[1].split("SKIPPED / INTAKE")[0]
    assert "#2373" not in failed_block
    assert "unknown_needs_rca" not in skipped_section[1]
    assert "forge diagnose --issue 2373" not in out


def test_digest_reports_accounting_and_disagreement_beside_the_row(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=_SKIP_REASON)
    _persist_rca(summary_path)
    out = _render_digest(tmp_path)

    assert "cost unmeasured for this story" in out
    assert "inconsistent:" in out
    assert "PLAN_REVIEW" in out


def test_unrecorded_skip_reason_also_renders_under_skipped(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, error=None)
    _persist_rca(summary_path)
    out = _render_digest(tmp_path)

    assert "skip_reason_unrecorded" in out.split("SKIPPED / INTAKE", 1)[1]
    assert "forge diagnose --issue 2373" not in out


# ── Summary writer seam: the canonical skip reason reaches the row ───────────


def test_summary_projects_canonical_skip_reason_onto_the_story_row(tmp_path: Path) -> None:
    """The sprint's own sentence must travel with the outcome it explains.

    The canonical ``SprintStoryState`` holds both the terminal SKIPPED and the
    reason the sprint recorded with it, while the per-story row was built from
    the coordinator result (which succeeded at PLAN_REVIEW and recorded no
    error). Projecting the outcome without the reason is what left a bare
    ``SKIPPED`` for every downstream reader to explain from something else.
    """
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.sprint.audit import _write_sprint_summary
    from theforge.sprint.manifest import ResolvedSprint, SprintResult
    from theforge.sprint.story_state import SprintStoryState, StoryOutcome

    state = SprintStoryState()
    state.register("issue-2373", "Issue #2373", canonical_ref="issue:2373")
    state.transition("issue-2373", outcome=StoryOutcome.SKIPPED, reason=_SKIP_REASON)

    coord_state = CoordinatorState(
        phase=Phase.PLAN_REVIEW,
        started_at="2026-08-11T02:00:00Z",
        workspace_path=tmp_path,
        log_dir=tmp_path,
    )
    coord_result = CoordinatorResult(
        success=True, phase=Phase.PLAN_REVIEW, state=coord_state, message="plan approved"
    )
    sprint_res = SprintResult(
        name="v0.13",
        specs_total=1,
        specs_succeeded=0,
        specs_failed=0,
        specs_skipped=1,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[("issue:2373", coord_result)],
    )
    manifest = ResolvedSprint(name="v0.13", budget_usd=10.0, stories=[], max_parallel=1)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = datetime.datetime.now(datetime.timezone.utc)

    _write_sprint_summary(
        manifest=manifest,
        result=sprint_res,
        canonical_refs=["issue:2373"],
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=log_dir,
        slug_map={"issue:2373": "issue-2373"},
        tasks_by_slug={"issue-2373": MagicMock(depends_on=[])},
        story_state=state,
    )

    summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text())
    row = {s["slug"]: s for s in summary["stories"]}["issue-2373"]
    assert row["outcome"] == "SKIPPED"
    assert row["error"] == _SKIP_REASON

    # And the row now classifies as the dependency skip it is.
    entry = _entry(log_dir / "sprint-summary.yaml")
    assert entry["primary_failure_class"] == "dependency_skip"
    assert "forge diagnose" not in _actions_text(entry)


def test_summary_never_overwrites_a_cause_the_story_recorded(tmp_path: Path) -> None:
    """A story that recorded its own error keeps it when canonical says SKIPPED."""
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.sprint.audit import _write_sprint_summary
    from theforge.sprint.manifest import ResolvedSprint, SprintResult
    from theforge.sprint.story_state import SprintStoryState, StoryOutcome

    state = SprintStoryState()
    state.register("issue-2373", "Issue #2373", canonical_ref="issue:2373")
    state.transition("issue-2373", outcome=StoryOutcome.SKIPPED, reason=_SKIP_REASON)

    coord_state = CoordinatorState(
        phase=Phase.ESCALATE,
        started_at="2026-08-11T02:00:00Z",
        workspace_path=tmp_path,
        log_dir=tmp_path,
    )
    coord_state.error = "agent credential rejected"
    coord_result = CoordinatorResult(
        success=False, phase=Phase.ESCALATE, state=coord_state, message="stopped"
    )
    sprint_res = SprintResult(
        name="v0.13",
        specs_total=1,
        specs_succeeded=0,
        specs_failed=0,
        specs_skipped=1,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[("issue:2373", coord_result)],
    )
    manifest = ResolvedSprint(name="v0.13", budget_usd=10.0, stories=[], max_parallel=1)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = datetime.datetime.now(datetime.timezone.utc)

    _write_sprint_summary(
        manifest=manifest,
        result=sprint_res,
        canonical_refs=["issue:2373"],
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=log_dir,
        slug_map={"issue:2373": "issue-2373"},
        tasks_by_slug={"issue-2373": MagicMock(depends_on=[])},
        story_state=state,
    )

    summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text())
    row = {s["slug"]: s for s in summary["stories"]}["issue-2373"]
    assert row["error"] == "agent credential rejected"
