"""Synthetic audit records for the knowledge-effectiveness tests (#1867).

Shared by the signals, metrics, and renderer mirrors so all three describe the
same record shape. A drift in what an audit record looks like should break one
builder, not three copies of it.
"""

from __future__ import annotations


def manifests(
    cohort: str,
    *,
    phase: str = "dev",
    index_state: str | None = "ready",
    include_index_state: bool = True,
) -> list[dict]:
    """Context manifests that put a run in the requested cohort."""
    if cohort == "unclassified":
        prior_run_context = {
            "enabled": False,
            "included": [],
            "dropped": [],
            "index_state": None,
            "note": "prior-run context disabled (knowledge.prior_run_context)",
        }
        if not include_index_state:
            prior_run_context.pop("index_state")
        return [
            {
                "phase": phase,
                "prior_run_context": prior_run_context,
            }
        ]
    included = (
        [{"run_id": "prior-1", "reason": "file_overlap", "score": 14}] if cohort == "with" else []
    )
    prior_run_context = {
        "enabled": True,
        "included": included,
        "dropped": [],
        "index_state": index_state,
        "note": "note",
    }
    if not include_index_state:
        prior_run_context.pop("index_state")
    return [
        {
            "phase": phase,
            "prior_run_context": prior_run_context,
        }
    ]


def review_loop(*, restated: int, novel: int) -> list[dict]:
    return [
        {
            "iteration": 1,
            "verdict": "APPROVE",
            "novel_findings": novel,
            "restated_findings": restated,
        }
    ]


def record(
    run_id: str,
    *,
    cohort: str = "with",
    success: bool = True,
    cost: float | None = 4.0,
    plan_regenerated: bool = False,
    restated: int = 0,
    novel: int = 2,
    dev_iterations: int = 1,
    review_cycles: int = 1,
    work_type: str = "feature",
    complexity: str = "medium",
    domains: tuple[str, ...] = ("backend",),
    started_at: str = "2026-08-01T10:00:00+00:00",
) -> dict:
    """One migrated audit record, shaped like ``generate_audit_log`` emits."""
    return {
        "run_id": run_id,
        "task": {"slug": run_id, "name": run_id},
        "timing": {"started_at": started_at},
        "outcome": {"success": success, "final_phase": "DONE"},
        "cost": {"total_usd": cost},
        "preflight": {
            "work_type": work_type,
            "complexity": complexity,
            "complexity_score": 5,
            "domains": list(domains),
        },
        "context_manifests": manifests(cohort),
        "plan_review": {"decision": "approve", "regenerated": plan_regenerated},
        "iterations": {
            "dev_iterations_productive": dev_iterations,
            "review_cycles_total": review_cycles,
            "review_loop": review_loop(restated=restated, novel=novel),
        },
    }


def cohorts(with_kwargs: dict, without_kwargs: dict, *, count: int = 3) -> list[dict]:
    """``count`` runs per cohort, all sharing one comparability bucket."""
    records: list[dict] = []
    for i in range(count):
        records.append(record(f"with-{i}", cohort="with", **with_kwargs))
        records.append(record(f"without-{i}", cohort="without", **without_kwargs))
    return records
