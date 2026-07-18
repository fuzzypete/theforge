"""Digest coverage for the shape-gate skip categories section (issue #1453 AC2/AC3).

The postmortem digest must distinguish skip categories in operator-facing output
and flag stuck-issue patterns. These tests exercise the renderer against a
summary carrying a ``shape_gate_skips`` block.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import yaml


def _write_summary(tmp_path: Path, run_id: str, *, skip_block: dict | None, stories: list) -> None:
    log_dir = tmp_path / ".forge" / "logs" / "v0.11"
    log_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "sprint": {
            "name": "v0.11",
            "run_id": run_id,
            "total_cost_usd": 1.0,
            "duration_seconds": 60.0,
            "finished_at": "2026-05-08T03:00:00Z",
        },
        "stories": stories,
    }
    if skip_block is not None:
        data["shape_gate_skips"] = skip_block
    (log_dir / "sprint-summary.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _render(tmp_path: Path, run_id: str) -> str:
    from theforge.cli.sprint_digest import display_sprint_digest

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        rc = display_sprint_digest(run_id, tmp_path)
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


def _landed(num: int) -> dict:
    return {"slug": f"issue-{num}", "path": f"Issue #{num}", "outcome": "DONE", "cost_usd": 1.0}


def test_skip_categories_render_grouped(tmp_path: Path) -> None:
    block = {
        "threshold": 3,
        "total": 2,
        "category_counts": {"blocked_by_stale_label": 1, "blocked_by_semantic_gate": 1},
        "categories": {
            "blocked_by_stale_label": [
                {
                    "issue_id": "7",
                    "reason_code": "needs_grooming_label",
                    "four_question_axis": "verification_passed_but_other_gate_fired",
                }
            ],
            "blocked_by_semantic_gate": [
                {
                    "issue_id": "1135",
                    "reason_code": "reopened_stale_contract",
                    "four_question_axis": "response_not_yet_attempted",
                }
            ],
        },
        "stuck_issues": [],
    }
    _write_summary(tmp_path, "run-1", skip_block=block, stories=[_landed(1)])
    out = _render(tmp_path, "run-1")

    assert "SHAPE-GATE SKIPS (2)" in out
    assert "blocked-by-stale-label (1)" in out
    assert "blocked-by-semantic-gate (1)" in out
    assert "#1135" in out
    assert "reopened_stale_contract" in out
    assert "response_not_yet_attempted" in out


def test_stuck_issue_flagged(tmp_path: Path) -> None:
    block = {
        "threshold": 3,
        "total": 1,
        "category_counts": {"blocked_by_semantic_gate": 1},
        "categories": {
            "blocked_by_semantic_gate": [
                {"issue_id": "1135", "reason_code": "reopened_stale_contract"}
            ]
        },
        "stuck_issues": [
            {
                "issue_id": "1135",
                "reason_code": "reopened_stale_contract",
                "block_count": 4,
                "first_seen": "2026-05-04T00:00:00Z",
                "last_seen": "2026-05-08T00:00:00Z",
                "run_ids": ["a", "b", "c", "d"],
            }
        ],
    }
    _write_summary(tmp_path, "run-1", skip_block=block, stories=[_landed(1)])
    out = _render(tmp_path, "run-1")

    assert "STUCK: #1135" in out
    assert "4×" in out
    assert "2026-05-04" in out


def test_skip_section_renders_for_all_done_sprint(tmp_path: Path) -> None:
    # Every story landed, but issues were skipped at the gate — the section
    # must still render (not hidden by the all-DONE early return).
    block = {
        "threshold": 3,
        "total": 1,
        "category_counts": {"unrunnable_by_shape": 1},
        "categories": {"unrunnable_by_shape": [{"issue_id": "5", "reason_code": "missing_type"}]},
        "stuck_issues": [],
    }
    _write_summary(tmp_path, "run-1", skip_block=block, stories=[_landed(1)])
    out = _render(tmp_path, "run-1")
    assert "SHAPE-GATE SKIPS" in out
    assert "unrunnable-by-shape" in out


def test_no_skip_block_renders_nothing(tmp_path: Path) -> None:
    _write_summary(tmp_path, "run-1", skip_block=None, stories=[_landed(1)])
    out = _render(tmp_path, "run-1")
    assert "SHAPE-GATE SKIPS" not in out
