"""Tests for the completed-sprint postmortem digest renderer.

The digest reads two on-disk artifacts — ``sprint-summary.yaml`` (LANDED
accounting) and ``sprint-rca.yaml`` (recovery signals) — and renders a recovery
brief grouped by outcome class. It never invokes the RCA engine. These tests
exercise the renderer directly and the ``forge status`` routing seam that
selects the digest for completed sprints and the telemetry table for live ones.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import yaml

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _write_summary(
    tmp_path: Path,
    sprint_name: str,
    run_id: str,
    stories: list[dict],
    *,
    total_cost_usd: float = 32.67,
    duration_seconds: float = 9720.0,
) -> Path:
    log_dir = tmp_path / ".forge" / "logs" / sprint_name
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / "sprint-summary.yaml"
    data = {
        "sprint": {
            "name": sprint_name,
            "run_id": run_id,
            "total_cost_usd": total_cost_usd,
            "duration_seconds": duration_seconds,
            "finished_at": "2026-05-08T03:00:00Z",
        },
        "stories": stories,
    }
    summary_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return summary_path


def _write_rca(tmp_path: Path, sprint_name: str, run_id: str, rca_stories: dict) -> Path:
    log_dir = tmp_path / ".forge" / "logs" / sprint_name
    log_dir.mkdir(parents=True, exist_ok=True)
    rca_path = log_dir / "sprint-rca.yaml"
    data = {
        "schema_version": 1,
        "ruleset_version": 1,
        "sprint_run_id": run_id,
        "generated_at": "2026-05-08T03:00:00Z",
        "generator": "mechanical",
        "stories": rca_stories,
    }
    rca_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return rca_path


def _render(tmp_path: Path, run_id: str) -> str:
    from theforge.cli.sprint_digest import display_sprint_digest

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        rc = display_sprint_digest(run_id, tmp_path)
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


def _landed_story(num: int, cost: float) -> dict:
    return {
        "slug": f"issue-{num}",
        "path": f"Issue #{num}",
        "outcome": "DONE",
        "cost_usd": cost,
        "started_at": "2026-05-08T00:00:00Z",
        "finished_at": "2026-05-08T00:10:00Z",
    }


# ── Full digest layout ────────────────────────────────────────────────────────


def _seed_full_sprint(tmp_path: Path) -> tuple[str, str]:
    name = "issues-1324,1325,1326,793,1101"
    run_id = "6c83b3061455"
    stories = [
        _landed_story(1101, 3.70),
        _landed_story(1325, 3.80),
        {
            "slug": "issue-1324",
            "path": "Issue #1324",
            "outcome": "FAILED",
            "cost_usd": 19.74,
            "started_at": "2026-05-08T00:00:00Z",
            "finished_at": "2026-05-08T00:34:00Z",
        },
        {
            "slug": "issue-1326",
            "path": "Issue #1326",
            "outcome": "FAILED",
            "cost_usd": 5.43,
            "started_at": "2026-05-08T00:00:00Z",
            "finished_at": "2026-05-08T01:00:00Z",
        },
        {
            "slug": "issue-793",
            "path": "Issue #793",
            "outcome": "DROPPED_SHAPE",
            "cost_usd": 0.0,
        },
    ]
    rca_stories = {
        "issue-1324": {
            "primary_failure_class": "provider_quota",
            "contributing_factors": ["fallback_not_applied", "operator_gate_timeout"],
            "evidence": [
                {
                    "source": "issue-1324/review-cycle-2/openai.yaml",
                    "rule_id": "provider_usage_limit",
                    "excerpt": "codex openai usage limit reached",
                },
                {
                    "source": "run-6c83b3061455.log",
                    "rule_id": "pending_decision_auto_rejected",
                    "excerpt": "pending decision timed out after 3600s",
                },
                {
                    "source": "run-6c83b3061455-summary.yaml",
                    "rule_id": "captured_outcome",
                    "excerpt": "outcome=FAILED",
                },
            ],
            "partial_value": [],
            "recommended_next_actions": [
                "wait for quota reset or switch the provider/model, then re-sprint #1324",
                "wire the provider fallback so the next failure recovers automatically",
            ],
        },
        "issue-1326": {
            "primary_failure_class": "worker_timeout",
            "contributing_factors": ["oversized_validation_scope"],
            "evidence": [
                {
                    "source": "run-6c83b3061455.log",
                    "rule_id": "worker_thread_timeout",
                    "excerpt": "worker thread timed out after 3600s",
                },
                {
                    "source": "run-6c83b3061455-summary.yaml",
                    "rule_id": "captured_outcome",
                    "excerpt": "outcome=FAILED",
                },
            ],
            "partial_value": [
                "runbook produced; read-only validations executed",
                "#1437 filed",
            ],
            "recommended_next_actions": [
                "inspect the worker log for the phase #1326 was in at timeout",
            ],
        },
        "issue-793": {
            "primary_failure_class": "intake_shape",
            "contributing_factors": [],
            "evidence": [
                {
                    "source": "run-6c83b3061455-summary.yaml",
                    "rule_id": "intake_dropped_shape",
                    "excerpt": "implementation_plan_in_body",
                },
                {
                    "source": "run-6c83b3061455-summary.yaml",
                    "rule_id": "captured_outcome",
                    "excerpt": "outcome=DROPPED_SHAPE",
                },
            ],
            "partial_value": [],
            "recommended_next_actions": [
                "reshape the #793 issue body to satisfy the intake gate, then re-run",
            ],
        },
    }
    _write_summary(tmp_path, name, run_id, stories)
    _write_rca(tmp_path, name, run_id, rca_stories)
    return name, run_id


def test_digest_full_layout_has_all_sections(tmp_path: Path) -> None:
    _name, run_id = _seed_full_sprint(tmp_path)
    output = _render(tmp_path, run_id)

    # Header
    assert "SPRINT issues-1324,1325,1326,793,1101" in output
    assert "completed" in output
    assert "$32.67" in output

    # LANDED
    assert "LANDED (2 of 5)" in output
    assert "✓ #1101" in output
    assert "✓ #1325" in output

    # FAILED — literal class headings
    assert "FAILED — provider_quota (1)" in output
    assert "FAILED — worker_timeout (1)" in output
    assert "✗ #1324" in output
    assert "contributing: fallback_not_applied, operator_gate_timeout" in output
    # evidence shown, baseline captured_outcome suppressed
    assert "codex openai usage limit reached at issue-1324/review-cycle-2/openai.yaml" in output
    assert "outcome=FAILED at" not in output

    # SKIPPED / INTAKE
    assert "SKIPPED / INTAKE (1)" in output
    assert "⊘ #793" in output
    assert "intake_shape — implementation_plan_in_body" in output

    # PARTIAL VALUE
    assert "PARTIAL VALUE (1)" in output
    assert "◐ #1326  runbook produced; read-only validations executed; #1437 filed" in output

    # NEXT
    assert "NEXT" in output
    assert "- reshape the #793 issue body to satisfy the intake gate, then re-run" in output


def test_intake_story_only_in_skipped_not_failed(tmp_path: Path) -> None:
    """An intake_shape story routes to SKIPPED / INTAKE, never a FAILED heading."""
    _name, run_id = _seed_full_sprint(tmp_path)
    output = _render(tmp_path, run_id)
    assert "FAILED — intake_shape" not in output


def test_failed_heading_is_literal_primary_class(tmp_path: Path) -> None:
    """The FAILED heading is the classifier's literal string, not a re-mapping."""
    name = "sprint-x"
    run_id = "runX"
    stories = [
        _landed_story(1, 1.0),
        {"slug": "issue-2", "path": "Issue #2", "outcome": "FAILED", "cost_usd": 2.0},
    ]
    rca_stories = {
        "issue-2": {
            "primary_failure_class": "merge_arming_failed",
            "contributing_factors": [],
            "evidence": [],
            "partial_value": [],
            "recommended_next_actions": [],
        }
    }
    _write_summary(tmp_path, name, run_id, stories)
    _write_rca(tmp_path, name, run_id, rca_stories)
    output = _render(tmp_path, run_id)
    assert "FAILED — merge_arming_failed (1)" in output


def test_partial_value_appears_in_addition_to_failed(tmp_path: Path) -> None:
    """A story with partial_value shows under both FAILED and PARTIAL VALUE."""
    _name, run_id = _seed_full_sprint(tmp_path)
    output = _render(tmp_path, run_id)
    # #1326 is a worker_timeout FAILED story AND has partial value.
    assert "FAILED — worker_timeout (1)" in output
    failed_idx = output.index("FAILED — worker_timeout")
    partial_idx = output.index("PARTIAL VALUE")
    assert output.index("✗ #1326") > 0
    assert output.index("◐ #1326") > partial_idx > failed_idx


def test_next_dedupes_and_cites_originating_stories(tmp_path: Path) -> None:
    """Identical actions collapse; a shared action cites both origin stories."""
    name = "dedup-sprint"
    run_id = "runD"
    stories = [
        {"slug": "issue-10", "path": "Issue #10", "outcome": "FAILED", "cost_usd": 1.0},
        {"slug": "issue-11", "path": "Issue #11", "outcome": "FAILED", "cost_usd": 1.0},
    ]
    shared = "wire the provider fallback so the next failure recovers automatically"
    rca_stories = {
        "issue-10": {
            "primary_failure_class": "provider_quota",
            "contributing_factors": [],
            "evidence": [],
            "partial_value": [],
            "recommended_next_actions": [shared],
        },
        "issue-11": {
            "primary_failure_class": "provider_quota",
            "contributing_factors": [],
            "evidence": [],
            "partial_value": [],
            "recommended_next_actions": [shared],
        },
    }
    _write_summary(tmp_path, name, run_id, stories)
    _write_rca(tmp_path, name, run_id, rca_stories)
    output = _render(tmp_path, run_id)

    # The shared action appears exactly once, citing both origin stories.
    assert output.count(f"- {shared}") == 1
    assert f"- {shared} (from #10, #11)" in output


def test_next_omits_citation_when_action_names_its_story(tmp_path: Path) -> None:
    name = "cite-sprint"
    run_id = "runC"
    stories = [{"slug": "issue-20", "path": "Issue #20", "outcome": "FAILED", "cost_usd": 1.0}]
    action = "reshape the #20 issue body to satisfy the intake gate, then re-run"
    rca_stories = {
        "issue-20": {
            "primary_failure_class": "provider_quota",
            "contributing_factors": [],
            "evidence": [],
            "partial_value": [],
            "recommended_next_actions": [action],
        }
    }
    _write_summary(tmp_path, name, run_id, stories)
    _write_rca(tmp_path, name, run_id, rca_stories)
    output = _render(tmp_path, run_id)
    assert f"- {action}" in output
    assert "(from #20)" not in output


# ── All-DONE tighter digest ───────────────────────────────────────────────────


def _already_satisfied_story(num: int) -> dict:
    """A no-merge, preflight-verdict ALREADY_DONE acceptance (issue #1937)."""
    return {
        "slug": f"issue-{num}",
        "path": f"Issue #{num}",
        "outcome": "ALREADY_DONE",
        "outcome_source": "preflight_verdict",
        "preflight_reason": "Working tree already satisfies the spec; git_state_match.",
        "merge": False,
        "landing": None,
        "cost_usd": 0.05,
        "started_at": "2026-05-08T00:00:00Z",
        "finished_at": "2026-05-08T00:01:00Z",
    }


def test_preflight_already_done_split_from_landed(tmp_path: Path) -> None:
    """A no-op ALREADY_DONE acceptance renders under ALREADY SATISFIED, not LANDED."""
    name = "already-satisfied"
    run_id = "runAS"
    stories = [
        _landed_story(1, 1.0),
        _already_satisfied_story(1879),
    ]
    _write_summary(tmp_path, name, run_id, stories)
    output = _render(tmp_path, run_id)

    # The merged land is the only LANDED story — the no-op acceptance is excluded.
    assert "LANDED (1 of 2)" in output
    assert "ALREADY SATISFIED (1)" in output
    assert "#1879" in output
    assert "no change needed" in output
    assert "git_state_match" in output
    # No recovery sections: a no-op acceptance still succeeded.
    assert "FAILED" not in output
    assert "forge rca" not in output


def test_resume_skip_merged_already_done_stays_landed(tmp_path: Path) -> None:
    """An ALREADY_DONE story that actually merged stays in LANDED, not split out."""
    name = "merged-already-done"
    run_id = "runMAD"
    merged_already_done = {
        "slug": "issue-42",
        "path": "Issue #42",
        "outcome": "ALREADY_DONE",
        "outcome_source": "resume_skip_merged",
        "merge": True,
        "cost_usd": 2.0,
        "started_at": "2026-05-08T00:00:00Z",
        "finished_at": "2026-05-08T00:10:00Z",
    }
    stories = [_landed_story(1, 1.0), merged_already_done]
    _write_summary(tmp_path, name, run_id, stories)
    output = _render(tmp_path, run_id)

    assert "LANDED (2 of 2)" in output
    assert "ALREADY SATISFIED" not in output


def test_all_done_renders_landed_only(tmp_path: Path) -> None:
    name = "all-done"
    run_id = "runAD"
    stories = [_landed_story(1, 1.0), _landed_story(2, 2.0)]
    _write_summary(tmp_path, name, run_id, stories)
    # No RCA file for an all-DONE sprint.
    output = _render(tmp_path, run_id)

    assert "LANDED (2 of 2)" in output
    assert "FAILED" not in output
    assert "SKIPPED" not in output
    assert "PARTIAL VALUE" not in output
    assert "NEXT" not in output
    assert "forge rca" not in output


# ── Missing RCA artifact ──────────────────────────────────────────────────────


def test_missing_rca_shows_pointer_and_landed_only(tmp_path: Path) -> None:
    name = "no-rca"
    run_id = "runNR"
    stories = [
        _landed_story(1, 1.0),
        {"slug": "issue-2", "path": "Issue #2", "outcome": "FAILED", "cost_usd": 2.0},
    ]
    _write_summary(tmp_path, name, run_id, stories)
    # Deliberately no sprint-rca.yaml written.
    output = _render(tmp_path, run_id)

    assert "LANDED (1 of 2)" in output
    assert f"forge rca {run_id}" in output
    assert "FAILED —" not in output
    assert "NEXT" not in output


def test_run_keyed_rca_preferred_over_pointer(tmp_path: Path) -> None:
    """A historical run resolves to its run-keyed RCA even if the pointer differs."""
    name = "hist-sprint"
    run_id = "runH"
    stories = [
        _landed_story(1, 1.0),
        {"slug": "issue-2", "path": "Issue #2", "outcome": "FAILED", "cost_usd": 2.0},
    ]
    _write_summary(tmp_path, name, run_id, stories)
    log_dir = tmp_path / ".forge" / "logs" / name

    # Pointer belongs to a *later* same-name run and must not be used.
    (log_dir / "sprint-rca.yaml").write_text(
        yaml.safe_dump(
            {
                "sprint_run_id": "runLATER",
                "stories": {
                    "issue-2": {
                        "primary_failure_class": "worker_timeout",
                        "contributing_factors": [],
                        "evidence": [],
                        "partial_value": [],
                        "recommended_next_actions": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    # Run-keyed durable file for this run.
    (log_dir / f"run-{run_id}-sprint-rca.yaml").write_text(
        yaml.safe_dump(
            {
                "sprint_run_id": run_id,
                "stories": {
                    "issue-2": {
                        "primary_failure_class": "provider_quota",
                        "contributing_factors": [],
                        "evidence": [],
                        "partial_value": [],
                        "recommended_next_actions": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    output = _render(tmp_path, run_id)
    assert "FAILED — provider_quota (1)" in output
    assert "worker_timeout" not in output


def test_pointer_from_different_run_is_rejected(tmp_path: Path) -> None:
    """A pointer belonging to a later run must not be read for the queried run.

    This is exactly the state ``forge rca <historical> --refresh`` leaves behind
    (write_pointer=False): the ``sprint-rca.yaml`` pointer reflects the latest
    run while an older run has no run-keyed file. The digest must fall through to
    the missing-RCA branch rather than misattribute the later run's failures.
    """
    name = "mismatch-sprint"
    run_id = "runOLD"
    stories = [
        _landed_story(1, 1.0),
        {"slug": "issue-2", "path": "Issue #2", "outcome": "FAILED", "cost_usd": 2.0},
    ]
    _write_summary(tmp_path, name, run_id, stories)
    log_dir = tmp_path / ".forge" / "logs" / name

    # Pointer belongs to a *later* run; no run-keyed file exists for runOLD.
    (log_dir / "sprint-rca.yaml").write_text(
        yaml.safe_dump(
            {
                "sprint_run_id": "runLATER",
                "stories": {
                    "issue-2": {
                        "primary_failure_class": "worker_timeout",
                        "contributing_factors": [],
                        "evidence": [],
                        "partial_value": [],
                        "recommended_next_actions": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    output = _render(tmp_path, run_id)
    # Falls through to missing-RCA: LANDED only + pointer, no misattributed class.
    assert "LANDED (1 of 2)" in output
    assert f"forge rca {run_id}" in output
    assert "worker_timeout" not in output
    assert "FAILED —" not in output


def test_digest_not_found_returns_1(tmp_path: Path) -> None:
    from theforge.cli.sprint_digest import display_sprint_digest

    buf = io.StringIO()
    with patch("sys.stderr", buf):
        rc = display_sprint_digest("nonexistent", tmp_path)
    assert rc == 1


# ── forge status routing seam ─────────────────────────────────────────────────


def test_status_routes_completed_sprint_to_digest(tmp_path: Path) -> None:
    """A completed sprint run selects the digest, not the telemetry table."""
    from theforge.cli.status import _render_status_blocks

    _name, run_id = _seed_full_sprint(tmp_path)

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        rc = _render_status_blocks([run_id], tmp_path)
    output = buf.getvalue()

    assert rc == 0
    # Digest markers present; telemetry-table column header absent.
    assert "LANDED (2 of 5)" in output
    assert "NEXT" in output
    assert "COMPLEXITY" not in output


def test_status_routes_live_sprint_to_telemetry_table(tmp_path: Path) -> None:
    """A live sprint (PID present) keeps the wide telemetry table."""
    from theforge.cli.status import _render_status_blocks

    name = "live-sprint"
    run_id = "live1"
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.pid").write_text("99999\nlive-sprint\n")
    state_path = runs_dir / f"{run_id}.state"
    state_path.write_text(
        yaml.safe_dump(
            {
                "sprint_name": name,
                "stories": [
                    {
                        "slug": "issue-1",
                        "path": "Issue #1",
                        "status": "running",
                        "phase": "DEV",
                        "cost_usd": 0.1,
                        "bundle_candidate": False,
                        "blocked_by": [],
                        "detail": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        rc = _render_status_blocks([run_id], tmp_path)
    output = buf.getvalue()

    assert rc == 0
    # Telemetry table markers present; digest markers absent.
    assert "COMPLEXITY" in output
    assert "[live]" in output
    assert "LANDED" not in output


def test_is_completed_sprint_detection(tmp_path: Path) -> None:
    from theforge.cli.status import _is_completed_sprint

    name = "done-sprint"
    run_id = "runDS"
    _write_summary(tmp_path, name, run_id, [_landed_story(1, 1.0)])
    assert _is_completed_sprint(run_id, tmp_path) is True

    # A PID file (live) flips it back to False.
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.pid").write_text("1\ndone-sprint\n")
    assert _is_completed_sprint(run_id, tmp_path) is False
