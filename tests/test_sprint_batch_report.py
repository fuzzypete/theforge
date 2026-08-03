"""Tests for post-sprint batchability analytics (``forge batch-report``).

The report is a pure function of on-disk sprint artifacts, so every test writes
a real ``sprint-summary.yaml`` plus per-story ``audit.yaml`` / ``preflight.yaml``
and reads the report back — the same path the CLI takes.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from theforge.sprint.batch_report import (
    build_batch_report,
    render_terminal,
    report_payload,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SPRINT = "batch-trial"
RUN_ID = "abc123"


def _story_row(slug: str, **overrides) -> dict:
    row = {
        "slug": slug,
        "path": f"Issue #{slug.rsplit('-', 1)[-1]}",
        "outcome": "DONE",
        "cost_usd": 1.0,
        "depends_on": [],
        "iteration_usage": {"dev": {"used": 1}, "review": {"used": 1}},
    }
    row.update(overrides)
    return row


def _write_sprint(tmp_path: Path, stories: list[dict], *, total_cost_usd: float = 10.0) -> Path:
    log_dir = tmp_path / ".forge" / "logs" / SPRINT
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / "sprint-summary.yaml"
    summary_path.write_text(
        yaml.safe_dump(
            {
                "sprint": {
                    "name": SPRINT,
                    "run_id": RUN_ID,
                    "total_cost_usd": total_cost_usd,
                },
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return summary_path


def _write_story_artifacts(
    tmp_path: Path,
    slug: str,
    *,
    phase_costs: dict | None = None,
    work_type: str = "bug",
    complexity: str = "small",
    sufficiency: str = "implementation_ready",
    likely_files: list[str] | None = None,
    files_changed: list[str] | None = None,
    dev_attempts: int = 1,
    review_cycles: int = 1,
    escalation: dict | None = None,
    error: str | None = None,
) -> None:
    story_dir = tmp_path / ".forge" / "logs" / SPRINT / slug
    story_dir.mkdir(parents=True, exist_ok=True)

    costs = {
        "preflight": 0.20,
        "plan": None,
        "plan_review": None,
        "dev": 0.60,
        "validate": 0.0,
        "review": 0.40,
    }
    costs.update(phase_costs or {})
    phases: dict[str, dict | None] = {}
    for phase, cost in costs.items():
        # ``None`` here means "phase never ran" (no audit block at all); an
        # explicitly unmeasured cost is expressed as {"cost_usd": None}.
        phases[phase] = None if cost is None else {"cost_usd": cost, "outcome": "success"}

    audit = {
        "phases": phases,
        "preflight": {"work_type": work_type, "complexity": complexity},
        "iterations": {
            "dev_attempts_total": dev_attempts,
            "review_cycles_total": review_cycles,
            "dev_loop": [{"files_changed": files_changed or []}],
        },
    }
    if escalation is not None:
        audit["escalation"] = escalation
    if error is not None:
        audit["error"] = error
    (story_dir / "audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")

    (story_dir / "preflight.yaml").write_text(
        yaml.safe_dump(
            {
                "sufficiency": sufficiency,
                "likely_files": likely_files if likely_files is not None else [],
                "complexity": complexity,
            }
        ),
        encoding="utf-8",
    )


def _two_eligible(tmp_path: Path) -> Path:
    """Two small, independent, implementation-ready DONE stories."""
    summary = _write_sprint(tmp_path, [_story_row("issue-101"), _story_row("issue-102")])
    _write_story_artifacts(
        tmp_path,
        "issue-101",
        likely_files=["src/a.py", "tests/test_a.py"],
        files_changed=["src/a.py", "tests/test_a.py"],
        phase_costs={"preflight": 0.20, "dev": 0.60, "review": 0.40},
    )
    _write_story_artifacts(
        tmp_path,
        "issue-102",
        likely_files=["src/b.py"],
        files_changed=["src/b.py"],
        phase_costs={"preflight": 0.10, "dev": 0.30, "review": 0.25},
    )
    return summary


def _by_slug(report, slug):
    return next(s for s in report.stories if s.slug == slug)


# ── Eligibility ───────────────────────────────────────────────────────────────


def test_small_independent_ready_stories_qualify(tmp_path: Path) -> None:
    report = build_batch_report(_two_eligible(tmp_path))
    assert [s.slug for s in report.qualified] == ["issue-101", "issue-102"]
    assert report.disqualified == ()


def test_non_done_outcome_disqualifies(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101", outcome="FAILED")])
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/a.py"])

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert not story.eligible
    assert any("outcome=FAILED" in reason for reason in story.disqualifiers)


def test_merge_conflict_disqualifies_and_is_flagged(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101", outcome="MERGE_FAILED")])
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/a.py"])

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert story.conflicted is True
    assert not story.eligible
    assert any("conflicted" in reason for reason in story.disqualifiers)


def test_conflict_detected_from_error_text(tmp_path: Path) -> None:
    summary = _write_sprint(
        tmp_path, [_story_row("issue-101", error="merge conflict in src/a.py")]
    )
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/a.py"])

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert story.conflicted is True
    assert not story.eligible


def test_retry_disqualifies_and_is_flagged(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101")])
    _write_story_artifacts(
        tmp_path, "issue-101", likely_files=["src/a.py"], dev_attempts=3, review_cycles=2
    )

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert story.retried is True
    assert story.dev_iterations == 3
    assert story.review_cycles == 2
    assert not story.eligible
    assert any("retried (3 dev iterations, 2 review cycles)" in r for r in story.disqualifiers)


def test_escalation_disqualifies_and_is_flagged(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101", outcome="ESCALATE")])
    _write_story_artifacts(
        tmp_path, "issue-101", likely_files=["src/a.py"], escalation={"reason": "stuck"}
    )

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert story.escalated is True
    assert not story.eligible
    assert "escalated" in story.disqualifiers


def test_dependency_edge_disqualifies(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101", depends_on=["issue-100"])])
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/a.py"])

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert not story.eligible
    assert any("dependency edge" in reason for reason in story.disqualifiers)


def test_preflight_gates_disqualify(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101")])
    _write_story_artifacts(
        tmp_path,
        "issue-101",
        complexity="medium",
        work_type="feature",
        sufficiency="needs_planning",
        likely_files=[],
    )

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert not story.eligible
    # Every failing gate is reported, not just the first one.
    joined = " | ".join(story.disqualifiers)
    assert "complexity=medium" in joined
    assert "work_type=feature" in joined
    assert "sufficiency=needs_planning" in joined
    assert "unknown touched-file footprint" in joined


def test_wide_footprint_disqualifies(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101")])
    _write_story_artifacts(tmp_path, "issue-101", likely_files=[f"src/f{i}.py" for i in range(9)])

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert not story.eligible
    assert any("touches 9 files (limit 6)" in r for r in story.disqualifiers)


# ── Files touched / independence ──────────────────────────────────────────────


def test_files_touched_unions_dev_iterations(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101")])
    story_dir = tmp_path / ".forge" / "logs" / SPRINT / "issue-101"
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/a.py"])
    audit = yaml.safe_load((story_dir / "audit.yaml").read_text(encoding="utf-8"))
    audit["iterations"]["dev_loop"] = [
        {"files_changed": ["src/a.py", "tests/test_a.py"]},
        {"files_changed": ["src/a.py", "docs/a.md"]},
    ]
    (story_dir / "audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert story.files_touched == ("docs/a.md", "src/a.py", "tests/test_a.py")


def test_overlapping_footprints_are_not_grouped(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101"), _story_row("issue-102")])
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/shared.py"])
    _write_story_artifacts(tmp_path, "issue-102", likely_files=["src/shared.py"])

    report = build_batch_report(summary)
    assert len(report.qualified) == 2
    # Both qualify individually, but overlapping files make this a conflict-bundle
    # question rather than a cost question — so no batch group forms.
    assert report.groups == ()


# ── Grouping and cost model ───────────────────────────────────────────────────


def test_group_reports_actual_and_estimated_cost(tmp_path: Path) -> None:
    report = build_batch_report(_two_eligible(tmp_path))
    assert len(report.groups) == 1
    group = report.groups[0]

    assert group.members == ("issue-101", "issue-102")
    assert group.group_id == "batch-issue-101"
    # actual = (0.20+0.60+0.40) + (0.10+0.30+0.25)
    assert group.actual_combined_cost_usd == 1.85
    # batched = preflight summed (0.30) + max dev (0.60) + max review (0.40)
    assert group.hypothetical_batched_cost_usd == 1.30
    assert group.estimated_savings_usd == 0.55
    assert group.cheaper_if_batched is True
    assert group.cost_complete is True
    assert group.combined_files == ("src/a.py", "src/b.py", "tests/test_a.py")


def test_max_stories_caps_group_size(tmp_path: Path) -> None:
    rows = [_story_row(f"issue-10{i}") for i in range(1, 4)]
    summary = _write_sprint(tmp_path, rows)
    for i in range(1, 4):
        _write_story_artifacts(tmp_path, f"issue-10{i}", likely_files=[f"src/{i}.py"])

    report = build_batch_report(summary, max_stories=2, max_complexity_budget=5)
    assert [len(g.members) for g in report.groups] == [2]


def test_unmeasured_phase_cost_is_not_reported_as_free(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101")])
    _write_story_artifacts(
        tmp_path,
        "issue-101",
        likely_files=["src/a.py"],
        phase_costs={"preflight": 0.20, "dev": 0.60, "review": 0.40},
    )
    story_dir = tmp_path / ".forge" / "logs" / SPRINT / "issue-101"
    audit = yaml.safe_load((story_dir / "audit.yaml").read_text(encoding="utf-8"))
    audit["phases"]["review"] = {"cost_usd": None, "outcome": "approve"}
    (story_dir / "audit.yaml").write_text(yaml.safe_dump(audit), encoding="utf-8")

    story = _by_slug(build_batch_report(summary), "issue-101")
    assert story.phase_costs["review"] is None
    assert story.cost_complete is False
    assert story.total_cost_usd == 0.80


def test_totals_aggregate_grouped_savings(tmp_path: Path) -> None:
    report = build_batch_report(_two_eligible(tmp_path))
    totals = report.totals
    assert totals["qualified_count"] == 2
    assert totals["grouped_story_count"] == 2
    assert totals["actual_grouped_cost_usd"] == 1.85
    assert totals["hypothetical_batched_cost_usd"] == 1.30
    assert totals["estimated_savings_usd"] == 0.55
    assert totals["cheaper_if_batched"] is True


# ── Rendering ─────────────────────────────────────────────────────────────────


def test_terminal_render_has_every_required_section(tmp_path: Path) -> None:
    summary = _write_sprint(
        tmp_path,
        [
            _story_row("issue-101"),
            _story_row("issue-102"),
            _story_row("issue-103", outcome="FAILED"),
        ],
    )
    _write_story_artifacts(
        tmp_path, "issue-101", likely_files=["src/a.py"], files_changed=["src/a.py"]
    )
    _write_story_artifacts(
        tmp_path, "issue-102", likely_files=["src/b.py"], files_changed=["src/b.py"]
    )
    _write_story_artifacts(
        tmp_path, "issue-103", likely_files=["src/c.py"], files_changed=["src/c.py"]
    )

    out = render_terminal(build_batch_report(summary))
    assert "BATCH ANALYTICS batch-trial" in out
    assert "QUALIFIED FOR BATCHING (2 of 3)" in out
    assert "DISQUALIFIED (1)" in out
    assert "BATCH GROUPS (1)" in out
    assert "actual combined:" in out
    assert "estimated if batched:" in out
    assert "(estimate)" in out
    assert "PER-STORY PHASE COSTS" in out
    assert "METHODOLOGY" in out
    # Files touched are rendered for both qualified and disqualified stories.
    assert "touched:   src/a.py" in out
    assert "touched:   src/c.py" in out


def test_terminal_render_notes_no_groups(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101")])
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/a.py"])
    out = render_terminal(build_batch_report(summary))
    assert "BATCH GROUPS (0)" in out
    assert "(none" in out


def test_payload_matches_report(tmp_path: Path) -> None:
    report = build_batch_report(_two_eligible(tmp_path))
    payload = report_payload(report)

    assert payload["schema_version"] == 1
    assert payload["sprint"]["run_id"] == RUN_ID
    assert payload["batch_rules"]["max_touched_files"] == 6
    assert "ESTIMATE" in payload["methodology"]
    assert [s["slug"] for s in payload["stories"]] == ["issue-101", "issue-102"]
    assert payload["stories"][0]["phase_costs_usd"]["dev"] == 0.60
    assert payload["stories"][0]["files_touched"] == ["src/a.py", "tests/test_a.py"]
    assert payload["groups"][0]["estimated_savings_usd"] == 0.55
    # Round-trips through both structured formats.
    assert yaml.safe_load(yaml.safe_dump(payload)) == payload
    assert json.loads(json.dumps(payload)) == payload


def test_rca_class_enriches_disqualified_story(tmp_path: Path) -> None:
    summary = _write_sprint(tmp_path, [_story_row("issue-101", outcome="FAILED")])
    _write_story_artifacts(tmp_path, "issue-101", likely_files=["src/a.py"])
    (tmp_path / ".forge" / "logs" / SPRINT / "sprint-rca.yaml").write_text(
        yaml.safe_dump(
            {
                "sprint_run_id": RUN_ID,
                "stories": {"issue-101": {"primary_failure_class": "gate_failure"}},
            }
        ),
        encoding="utf-8",
    )

    report = build_batch_report(summary, run_id=RUN_ID)
    assert _by_slug(report, "issue-101").rca_class == "gate_failure"
    assert "rca: gate_failure" in render_terminal(report)


def test_unreadable_summary_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    assert build_batch_report(path) is None


# ── CLI ───────────────────────────────────────────────────────────────────────


def _run_cli(tmp_path: Path, argv: list[str]) -> tuple[int, str]:
    """Parse through the real top-level parser, then dispatch the handler."""
    from theforge.cli.batch_report import cmd_batch_report
    from theforge.cli.main import build_parser

    (tmp_path / "forge.yaml").write_text(
        yaml.safe_dump({"project": {"root": str(tmp_path)}}), encoding="utf-8"
    )
    args = build_parser().parse_args(
        ["batch-report", *argv, "--config", str(tmp_path / "forge.yaml")]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_batch_report(args)
    return rc, buf.getvalue()


def test_cli_terminal_output(tmp_path: Path) -> None:
    _two_eligible(tmp_path)
    rc, out = _run_cli(tmp_path, [RUN_ID])
    assert rc == 0, out
    assert "BATCH ANALYTICS" in out
    assert "batch-issue-101" in out


def test_cli_yaml_output(tmp_path: Path) -> None:
    _two_eligible(tmp_path)
    rc, out = _run_cli(tmp_path, [RUN_ID, "--format", "yaml"])
    assert rc == 0, out
    payload = yaml.safe_load(out)
    assert payload["groups"][0]["members"] == ["issue-101", "issue-102"]


def test_cli_json_output(tmp_path: Path) -> None:
    _two_eligible(tmp_path)
    rc, out = _run_cli(tmp_path, [RUN_ID, "--format", "json"])
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["totals"]["estimated_savings_usd"] == 0.55


def test_cli_unknown_run_id_fails_explicitly(tmp_path: Path, capsys) -> None:
    _two_eligible(tmp_path)
    rc, _ = _run_cli(tmp_path, ["nope"])
    assert rc == 1
    assert "No sprint data found for run ID 'nope'" in capsys.readouterr().err


def test_cli_sensitivity_flags_change_grouping(tmp_path: Path) -> None:
    rows = [_story_row(f"issue-10{i}") for i in range(1, 4)]
    _write_sprint(tmp_path, rows)
    for i in range(1, 4):
        _write_story_artifacts(tmp_path, f"issue-10{i}", likely_files=[f"src/{i}.py"])

    rc, out = _run_cli(tmp_path, [RUN_ID, "--format", "json", "--max-stories", "2"])
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["batch_rules"]["max_stories"] == 2
    assert len(payload["groups"][0]["members"]) == 2
