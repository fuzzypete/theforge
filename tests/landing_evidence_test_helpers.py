"""Publish landing evidence the way a real landing does, for tests.

Since #2849 the substrate's landed query reads the projected landing assertion
rather than the flattened ``audit_records.landing_status`` column. A test that
wants a run to *be landed* therefore has to publish the artifact a real landing
publishes, not just stamp the column — the column is now the completion-time
snapshot it always was, and no longer the answer to "did this land".

Kept here rather than repeated per test file so the artifact shape has one
definition; it is built through ``landing_evidence.build_landing_assertion``, so
a change to what an assertion must name reaches every test at once.
"""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator.landing_evidence import (
    build_landing_assertion,
    write_landing_assertion,
)


def publish_landed(
    project_root: Path,
    run_id: str,
    *,
    slug: str = "demo",
    target_branch: str = "main",
    landing_mode: str = "merge-pr",
    observer: str = "sprint.queued-pr",
    observed_at: str = "2026-03-01T12:00:00+00:00",
) -> dict:
    """Publish a positive landing assertion for ``run_id`` and return it."""
    assertion = build_landing_assertion(
        run_id=run_id,
        slug=slug,
        landing_mode=landing_mode,
        target_branch=target_branch,
        reviewed_commit="reviewed-sha",
        gated_commit="gated-sha",
        carrier_kind="pull_request",
        carrier_ref="#1",
        landed_commit="landed-sha",
        observer=observer,
        observed_at=observed_at,
    )
    write_landing_assertion(project_root, assertion)
    return assertion
