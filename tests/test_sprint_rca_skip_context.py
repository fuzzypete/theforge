"""RCA coverage for shape-gate skip context attachment (issue #1453 AC4).

Sprint RCA output must attach the same skip-classification context to its
narrative so an operator reading an RCA sees whether the sprint was affected by
gate friction and how often. The engine sources it from the summary, keeping the
RCA a pure function over on-disk artifacts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.sprint.rca import build_sprint_rca


def _write_summary(tmp_path: Path, *, skip_block: dict | None) -> Path:
    log_dir = tmp_path / ".forge" / "logs" / "v0.11"
    log_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "sprint": {"name": "v0.11", "run_id": "run-1", "finished_at": "2026-05-08T03:00:00Z"},
        "stories": [
            {
                "slug": "issue-1",
                "path": "Issue #1",
                "outcome": "ESCALATE",
                "error": "review requested changes",
            }
        ],
    }
    if skip_block is not None:
        data["shape_gate_skips"] = skip_block
    summary_path = log_dir / "sprint-summary.yaml"
    summary_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return summary_path


def test_rca_attaches_skip_block(tmp_path: Path) -> None:
    block = {
        "threshold": 3,
        "total": 1,
        "category_counts": {"blocked_by_semantic_gate": 1},
        "categories": {
            "blocked_by_semantic_gate": [
                {"issue_id": "1135", "reason_code": "reopened_stale_contract"}
            ]
        },
        "stuck_issues": [],
    }
    summary_path = _write_summary(tmp_path, skip_block=block)
    payload = build_sprint_rca(summary_path)
    assert payload is not None
    assert payload["shape_gate_skips"] == block


def test_rca_omits_skip_block_when_absent(tmp_path: Path) -> None:
    summary_path = _write_summary(tmp_path, skip_block=None)
    payload = build_sprint_rca(summary_path)
    assert payload is not None
    assert "shape_gate_skips" not in payload


def test_rca_stays_reproducible_with_skip_block(tmp_path: Path) -> None:
    block = {
        "threshold": 3,
        "total": 1,
        "category_counts": {"unrunnable_by_shape": 1},
        "categories": {"unrunnable_by_shape": [{"issue_id": "5", "reason_code": "missing_type"}]},
        "stuck_issues": [],
    }
    summary_path = _write_summary(tmp_path, skip_block=block)
    # Two builds from the same inputs must be byte-identical (the --check guard
    # depends on this determinism).
    assert build_sprint_rca(summary_path) == build_sprint_rca(summary_path)
