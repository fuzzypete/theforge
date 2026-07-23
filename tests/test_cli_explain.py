"""Tests for `forge explain` — operator-facing routing_decision render (#270).

Covers the read-only view over the top-level routing_decision block (#1391):
per-role rendering, the not-checked / did-not-fire / fired tri-state, the
explicit_override_locked reason, the absent-block fallback, and the
substrate-backed --story lookup. No agent calls or profile reads happen here —
the view reads only the recorded block.
"""

from __future__ import annotations

import json
from pathlib import Path

from theforge.cli import explain
from theforge.coordinator import audit_substrate as sub


def _routing_block(*, dev_override: bool = False, reviewer_fired: bool = False) -> dict:
    """A representative routing_decision block covering every render branch."""
    dev_pool = [
        {
            "name": "opus",
            "tier": "strong",
            "included": True,
            "reason": "none",
            "signals": {
                "success_rate": {
                    "raw": 0.8,
                    "weighted": 0.8,
                    "runs": 12,
                    "floor": "pass",
                    "rate": 0.8,
                }
            },
        },
        {"name": "deepseek", "tier": "strong", "included": False, "reason": "auth_missing"},
    ]
    dev_reason = "explicit_override_locked" if dev_override else "none"
    if dev_override:
        dev_pool[0]["reason"] = dev_reason
    return {
        "origin": "preflight",
        "preflight": {
            "candidate_pool": [
                {"name": "sonnet", "tier": "mid", "included": True, "reason": "none"}
            ],
            "exploration": {"mode": "winner"},
            "final": {"model": "sonnet", "tier": "mid", "rationale": "[preflight] cheap"},
        },
        "planner": {
            "candidate_pool": [
                {"name": "opus", "tier": "strong", "included": True, "reason": "none"}
            ],
            "exploration": {"mode": "winner"},
            "final": {"model": "opus", "tier": "strong", "rationale": "[preflight] planner"},
        },
        "dev": {
            "score": 9,
            "base_tier_from_score": "strong",
            "candidate_pool": dev_pool,
            "promotion_check": {
                "mechanism": "_check_promotion",
                "fired": False,
                "outcome": "below_sample_floor",
                "model": "opus",
                "complexity": "HIGH",
                "raw_success_rate": None,
                "weighted_success_rate": None,
                "sample_size": 3,
                "tainted_runs": 0,
                "threshold": 0.60,
                "min_runs": 5,
                "floor": "fail",
                "resulting_tier": "strong",
            },
            "demotion_check": {
                "mechanism": "dev_recency_recovery",
                "applicable": False,
                "fired": False,
                "checked": {
                    "weighted_success_rate": None,
                    "threshold": 0.60,
                    "sample_size": 3,
                    "min_runs": 5,
                },
                "reason": "no_admissible_promotion_signal",
            },
            "post_plan_checkpoint": {"applied": False, "reason": "checkpoint_not_implemented_v1"},
            "persistent_p1_dev_escalation": {
                "mechanism": "persistent_p1_dev_escalation",
                "fired": True,
                "scope": "run",
                "return_path": "fresh_run_state_reset",
                "signal": {
                    "kind": "persistent_p1",
                    "review_cycle": 2,
                    "file": "src/cli.py",
                    "descriptions": ["cli.py never wires gate_override into TaskStory"],
                },
                "model_swap": {"from_model": "sonnet", "to_model": "opus"},
            },
            "exploration": {"mode": "winner"},
            "final": {"model": "opus", "tier": "strong", "rationale": "[preflight] dev on strong"},
        },
        "plan_review": {
            "candidate_pool": [
                {"name": "gpt", "tier": "strong", "included": True, "reason": "none"}
            ],
            "demotion_check": {
                "mechanism": "provider_health",
                "fired": False,
                "deprioritized": [],
                "fell_back": [],
                "reason": "no_unhealthy_candidates",
            },
            "exploration": {"mode": "winner"},
            "final": {"models": ["gpt-5.4"], "rationale": "[preflight] plan reviewer"},
        },
        "code_review": {
            "candidate_pool": [
                {"name": "opus", "tier": "strong", "included": True, "reason": "none"},
                {
                    "name": "gpt",
                    "tier": "strong",
                    "included": False,
                    "reason": "transport_unavailable",
                    "detail": "health_deprioritized",
                },
            ],
            "demotion_check": {
                "mechanism": "provider_health",
                "fired": reviewer_fired,
                "deprioritized": ["gpt"] if reviewer_fired else [],
                "fell_back": [],
                "reason": (
                    "health_deprioritized: gpt"
                    if reviewer_fired
                    else "no_unhealthy_candidates_in_pool"
                ),
            },
            "exploration": {"mode": "winner"},
            "final": {"models": ["opus"], "rationale": "[preflight] code reviewer"},
        },
    }


def test_render_covers_every_role_and_selection() -> None:
    lines = explain.render_routing_decision(_routing_block())
    text = "\n".join(lines)
    for header in ("PREFLIGHT", "PLANNER", "DEV", "PLAN REVIEW", "CODE REVIEW"):
        assert header in text
    # Selected agents and reviewer model lists both render.
    assert "selected: sonnet [mid]" in text
    assert "selected: opus" in text
    assert "gpt-5.4" in text


def test_render_signals_show_raw_weighted_and_floor() -> None:
    text = "\n".join(explain.render_routing_decision(_routing_block()))
    assert "raw=0.8000" in text
    assert "weighted=0.8000" in text
    assert "runs=12" in text
    assert "sample-floor pass" in text


def test_render_excluded_reason_is_scannable() -> None:
    """ "why was model X never a reviewer" → the excluded-reason line."""
    text = "\n".join(explain.render_routing_decision(_routing_block()))
    assert "✗ deepseek" in text
    assert "provider credentials missing" in text
    # The health-deprioritized reviewer exclusion carries its detail.
    assert "✗ gpt" in text
    assert "transport unavailable" in text
    assert "health_deprioritized" in text


def test_render_surfaces_score_policy_per_axis() -> None:
    """The #1019 score-to-routing policy is rendered per role, and the
    reasoning_effort axis is surfaced as intentionally not score-controlled."""
    from theforge.routing import axis_decision

    block = _routing_block()
    block["reasoning_effort"] = axis_decision("reasoning_effort", 9)
    block["dev"]["score_policy"] = {"dev_tier": axis_decision("dev_tier", 9)}
    block["planner"]["score_policy"] = {"plan_tier": axis_decision("plan_tier", 9)}
    count_axis = dict(axis_decision("reviewer_count", 9))
    count_axis["resolved_count"] = 3
    count_axis["seated_count"] = 1
    block["code_review"]["score_policy"] = {
        "reviewer_tier": axis_decision("plan_tier", 9),
        "reviewer_count": count_axis,
    }

    text = "\n".join(explain.render_routing_decision(block))
    assert "score policy:" in text
    assert "dev_tier: score=9 → bucket=strong range=[7, 10] output=strong" in text
    assert "reviewer_count:" in text
    assert "resolved_count=3" in text
    assert "seated=1" in text
    # reasoning_effort is surfaced top-level as deliberately excluded.
    assert "reasoning_effort: not score-controlled" in text


def test_tri_state_distinguishes_not_checked_from_did_not_fire() -> None:
    """not-checked vs checked-did-not-fire vs fired must be distinguishable."""
    # promotion checked but did not fire → ◐; recency-recovery return path not
    # applicable (below sample floor, no admissible signal) → ○.
    text = "\n".join(explain.render_routing_decision(_routing_block()))
    assert "promotion: checked, did not fire" in text
    assert "demotion (dev_recency_recovery): not checked" in text
    assert "post-plan checkpoint: not checked" in text

    # A reviewer health demotion that fired renders as fired.
    fired_text = "\n".join(explain.render_routing_decision(_routing_block(reviewer_fired=True)))
    assert "demotion (provider_health): fired" in fired_text


def test_render_surfaces_in_run_persistent_p1_escalation() -> None:
    text = "\n".join(explain.render_routing_decision(_routing_block()))
    assert "in-run persistent-P1 escalation: fired" in text
    assert "sonnet → opus" in text
    assert "scope=run" in text
    assert "return=fresh_run_state_reset" in text


def test_reviewer_health_no_op_renders_checked_did_not_fire() -> None:
    """A reviewer provider-health block that ran but found nothing to demote is a
    LIVE, checked mechanism (fired=False) — it must render as "checked, did not
    fire", never "not checked" (a present block is always a checked mechanism)."""
    text = "\n".join(explain.render_routing_decision(_routing_block()))
    # plan_review uses reason "no_unhealthy_candidates" with fired=False; that is
    # a no-op outcome of a mechanism that DID run.
    assert "demotion (provider_health): checked, did not fire" in text
    assert "demotion (provider_health): not checked" not in text


def test_reviewer_missing_block_renders_not_checked() -> None:
    """Only a wholly-absent reviewer demotion_check reads as "not checked"."""
    block = _routing_block()
    block["plan_review"].pop("demotion_check")
    lines = []
    explain._render_reviewer_mechanisms(block["plan_review"], lines)
    assert any("not checked" in line for line in lines)


def test_explicit_override_locked_reason_renders() -> None:
    text = "\n".join(explain.render_routing_decision(_routing_block(dev_override=True)))
    assert "locked by explicit forge.yaml override" in text


def test_explain_from_file_renders_block(tmp_path: Path, capsys) -> None:
    record = {"schema_version": 7, "routing_decision": _routing_block()}
    path = tmp_path / "run-x.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    class _Args:
        file = str(path)
        story = None
        run = None
        config = None

    rc = explain.cmd_explain(_Args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Routing decision (origin: preflight)" in out
    assert "DEV" in out


def test_absent_block_reports_explicitly(tmp_path: Path, capsys) -> None:
    """Pre-#1391 records (routing_decision None) must say so, not render empty."""
    record = {"schema_version": 6, "routing_decision": None}
    path = tmp_path / "run-old.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    class _Args:
        file = str(path)
        story = None
        run = None
        config = None

    rc = explain.cmd_explain(_Args())
    assert rc == 1
    err = capsys.readouterr().err
    assert "no routing_decision block recorded" in err
    assert "#1391" in err


def test_missing_block_key_reports_explicitly(tmp_path: Path, capsys) -> None:
    record = {"schema_version": 7}
    path = tmp_path / "run-nokey.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    class _Args:
        file = str(path)
        story = None
        run = None
        config = None

    assert explain.cmd_explain(_Args()) == 1
    assert "no routing_decision block recorded" in capsys.readouterr().err


def test_resolve_story_issue_number_vs_slug() -> None:
    assert explain._resolve_story("270") == (None, 270)
    assert explain._resolve_story("#270") == (None, 270)
    assert explain._resolve_story("issue-270") == ("issue-270", None)


def _write_run_and_substrate(project_root: Path, record: dict) -> None:
    """Persist a per-run JSON file and build the substrate over it (seam setup)."""
    (project_root / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    runs = sub.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{record['run_id']}.json").write_text(json.dumps(record), encoding="utf-8")
    sub.rebuild_from_runs(project_root)


def _story_record(run_id: str, slug: str, issue: int, started: str) -> dict:
    return {
        "schema_version": 7,
        "run_id": run_id,
        "task": {"slug": slug, "name": slug, "github_issue": issue},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": started, "duration_seconds": 60.0},
        "cost": {"total_usd": 1.0},
        "totals": {"cost_usd": 1.0},
        "routing_decision": _routing_block(),
    }


def test_explain_story_by_slug_via_substrate(tmp_path: Path, capsys) -> None:
    """Seam: per-run record → substrate → `forge explain --story <slug>`."""
    _write_run_and_substrate(
        tmp_path, _story_record("run-270", "issue-270", 270, "2026-07-20T10:00:00+00:00")
    )

    class _Args:
        file = None
        story = "issue-270"
        run = None
        config = str(tmp_path / "forge.yaml")

    assert explain.cmd_explain(_Args()) == 0
    out = capsys.readouterr().out
    assert "story issue-270" in out
    assert "Routing decision (origin: preflight)" in out


def test_explain_story_by_issue_number_returns_latest_run(tmp_path: Path, capsys) -> None:
    """A bare issue number resolves via issue_id and returns the newest run."""
    _write_run_and_substrate(
        tmp_path, _story_record("run-old", "issue-270", 270, "2026-07-19T10:00:00+00:00")
    )
    # Second, newer run for the same issue.
    runs = sub.runs_dir(tmp_path)
    newer = _story_record("run-new", "issue-270", 270, "2026-07-21T10:00:00+00:00")
    (runs / "run-new.json").write_text(json.dumps(newer), encoding="utf-8")
    sub.rebuild_from_runs(tmp_path)

    record = sub.latest_record_for(sub.require_substrate(tmp_path), issue_id=270)
    assert record["run_id"] == "run-new"

    class _Args:
        file = None
        story = "270"
        run = None
        config = str(tmp_path / "forge.yaml")

    assert explain.cmd_explain(_Args()) == 0
    assert "story 270" in capsys.readouterr().out


def test_explain_story_not_found(tmp_path: Path, capsys) -> None:
    _write_run_and_substrate(
        tmp_path, _story_record("run-270", "issue-270", 270, "2026-07-20T10:00:00+00:00")
    )

    class _Args:
        file = None
        story = "issue-999"
        run = None
        config = str(tmp_path / "forge.yaml")

    assert explain.cmd_explain(_Args()) == 1
    assert "no audit record found for story issue-999" in capsys.readouterr().err


def test_explain_story_does_not_rebuild_missing_substrate(tmp_path: Path, capsys) -> None:
    """Read-only guard: with per-run JSON present but the substrate absent,
    `forge explain --story` must NOT create/rebuild the index — it fails with an
    explicit rebuild instruction and leaves the filesystem unchanged."""
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")
    runs = sub.runs_dir(tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    rec = _story_record("run-270", "issue-270", 270, "2026-07-20T10:00:00+00:00")
    (runs / "run-270.json").write_text(json.dumps(rec), encoding="utf-8")
    # Substrate deliberately not built.
    sub_path = sub.substrate_path(tmp_path)
    assert not sub_path.exists()

    class _Args:
        file = None
        story = "issue-270"
        run = None
        config = str(tmp_path / "forge.yaml")

    assert explain.cmd_explain(_Args()) == 1
    err = capsys.readouterr().err
    assert "forge audits rebuild" in err
    # The command must not have written the index as a side effect.
    assert not sub_path.exists(), "explain must not rebuild the substrate"


def test_explain_run_leaves_stale_substrate_unchanged(tmp_path: Path, capsys) -> None:
    """Read-only guard: even when the substrate is stale (a per-run file changed
    after the index was built), `forge explain --run` reads the existing index
    without rewriting it — the file's mtime is unchanged."""
    _write_run_and_substrate(
        tmp_path, _story_record("run-270", "issue-270", 270, "2026-07-20T10:00:00+00:00")
    )
    sub_path = sub.substrate_path(tmp_path)
    # Make the substrate stale relative to the source per-run file.
    import os
    import time

    time.sleep(0.01)
    newer = _story_record("run-270", "issue-270", 270, "2026-07-21T10:00:00+00:00")
    (sub.runs_dir(tmp_path) / "run-270.json").write_text(json.dumps(newer), encoding="utf-8")
    before = os.stat(sub_path).st_mtime_ns

    class _Args:
        file = None
        story = None
        run = "run-270"
        config = str(tmp_path / "forge.yaml")

    assert explain.cmd_explain(_Args()) == 0
    after = os.stat(sub_path).st_mtime_ns
    assert before == after, "explain must not rewrite/rebuild the substrate index"


def test_explain_no_audit_inputs(tmp_path: Path, capsys) -> None:
    (tmp_path / "forge.yaml").write_text("project: test\n", encoding="utf-8")

    class _Args:
        file = None
        story = "issue-270"
        run = None
        config = str(tmp_path / "forge.yaml")

    assert explain.cmd_explain(_Args()) == 1
    assert "no audit records found" in capsys.readouterr().err


def test_file_not_found_reports(tmp_path: Path, capsys) -> None:
    class _Args:
        file = str(tmp_path / "nope.json")
        story = None
        run = None
        config = None

    assert explain.cmd_explain(_Args()) == 1
    assert "audit file not found" in capsys.readouterr().err
