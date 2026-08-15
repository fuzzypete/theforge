"""Shared fixtures for the #2348 structural-decay spike tests.

Both halves of the POC's test suite build the same two things: synthetic
``(run, path)`` touch rows and a coverage dict. They live here rather than in
either test module so the ranking tests (pure math) and the report tests
(substrate + rendering) can each construct a scenario without importing the
other.
"""

from __future__ import annotations


def touch_rows(
    run_id: str,
    paths: list[str],
    *,
    cost: float | None,
    complexity: int | None = 5,
    dev_model: str | None = "anthropic/opus",
    started_at: str = "2026-07-01T00:00:00Z",
    insertions: int = 10,
) -> list[dict]:
    """Return one touch row per path, shaped like ``changed_file_touch_rows``."""
    return [
        {
            "run_id": run_id,
            "path": path,
            "insertions": insertions,
            "deletions": 1,
            "binary": False,
            "slug": f"issue-{run_id}",
            "issue_id": None,
            "started_at": started_at,
            "total_cost_usd": cost,
            "complexity_score": complexity,
            "outcome_success": 1,
            "verdict": "APPROVE",
            "dev_model": dev_model,
            "dev_resolved_model": dev_model,
            "milestone": "v0.15.0",
        }
        for path in paths
    ]


def coverage(joinable: int, measured: int, *, spend: float = 100.0) -> dict:
    """Return a coverage dict shaped like ``changed_file_coverage``."""
    ratio = joinable / measured if measured else 0.0
    return {
        "measured_runs": measured,
        "measured_spend_usd": spend,
        "joinable_runs": joinable,
        "joinable_spend_usd": spend * ratio,
        "run_coverage_ratio": ratio,
        "spend_coverage_ratio": ratio,
        "first_joinable_at": "2026-07-01T00:00:00Z",
        "last_joinable_at": "2026-08-01T00:00:00Z",
    }


def seed_record(
    run_id: str,
    paths: list[str],
    *,
    cost: float | None,
    started_at: str,
    complexity_score: int | None = 5,
    reviews: list[dict] | None = None,
) -> dict:
    """Return an audit record carrying a changed-files block, for upsert."""
    record: dict = {
        "run_id": run_id,
        "task": {"slug": f"issue-{run_id}"},
        "timing": {"started_at": started_at},
        "cost": {"total_usd": cost},
        "outcome": {"success": True},
        "changed_files": {
            "base_ref": "a" * 40,
            "head_ref": "b" * 40,
            "files": [
                {"path": path, "insertions": 5, "deletions": 1, "binary": False} for path in paths
            ],
        },
    }
    if complexity_score is not None:
        record["preflight"] = {"complexity": "medium", "complexity_score": complexity_score}
    if reviews is not None:
        record["reviews"] = reviews
    return record
