"""Substrate loading, rendering and CLI for the #2348 spike (see package docstring).

The I/O half of the POC: opens the audit substrate strictly read-only, recovers
the controls that are not indexed columns, loads module line counts through the
configured hard conventions, and renders the report the spike record quotes.
The ranking math it feeds lives in :mod:`.ranking` and knows nothing about any
of this.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .ranking import (
    build_runs,
    compare_to_line_counts,
    rank_candidates,
    resolve_controls,
    threshold_status,
)

# ── Substrate loading (read-only) ────────────────────────────────────────


def _extras_from_records(conn, run_ids: set[str]) -> dict[str, dict]:
    """Recover the non-indexed controls by decoding only the runs we ranked.

    ``panel_size`` and ``review_cycles`` come from the ``reviews`` block and
    ``finding_files`` from each review's findings; none is an indexed column, so
    this is the ``derived`` availability tier. Runs whose record will not decode
    simply contribute nothing and end up reported as uncontrolled.
    """
    if not run_ids:
        return {}
    extras: dict[str, dict] = {}
    for row in conn.execute("SELECT run_id, raw_json FROM audit_records"):
        run_id = row[0]
        if run_id not in run_ids:
            continue
        try:
            record = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        reviews = record.get("reviews")
        if not isinstance(reviews, list):
            continue
        panel_sizes = [
            len(rv["pool_models"])
            for rv in reviews
            if isinstance(rv, dict) and isinstance(rv.get("pool_models"), list)
        ]
        files: list[str] = []
        for review in reviews:
            if not isinstance(review, dict):
                continue
            for finding in review.get("findings") or []:
                if isinstance(finding, dict) and isinstance(finding.get("file"), str):
                    files.append(finding["file"])
        extras[run_id] = {
            "panel_size": max(panel_sizes) if panel_sizes else None,
            "review_cycles": len(reviews),
            "finding_files": tuple(files),
        }
    return extras


def load_report(project_root: Path, *, since: str | None = None, top: int = 15) -> dict:
    """Build the full report from the substrate. Opens read-only; never writes."""
    from theforge.config.load import load_config  # noqa: PLC0415
    from theforge.coordinator.audit_read_model import (  # noqa: PLC0415
        changed_file_coverage,
        changed_file_touch_rows,
    )
    from theforge.coordinator.audit_storage import open_readonly  # noqa: PLC0415
    from theforge.line_count_conventions import module_line_counts  # noqa: PLC0415

    # open_readonly builds a file: URI, which a relative path cannot express.
    project_root = Path(project_root).resolve()
    conn = open_readonly(project_root)
    try:
        coverage = changed_file_coverage(conn, since=since)
        touch_rows = changed_file_touch_rows(conn, since=since)
        run_ids = {str(row["run_id"]) for row in touch_rows}
        extras = _extras_from_records(conn, run_ids)
    finally:
        conn.close()

    line_counts: dict[str, int] = {}
    config_path = project_root / "forge.yaml"
    if config_path.exists():
        try:
            hard = load_config(config_path).conventions_hard
            if hard is not None:
                line_counts = module_line_counts(hard, project_root)
        except Exception as exc:  # noqa: BLE001 - a POC reports, it does not abort
            print(f"warning: could not load line counts ({exc})", file=sys.stderr)

    runs = build_runs(touch_rows, record_extras=extras)
    # Rank only paths that are modules in the configured package roots. Tests
    # are excluded deliberately: a test file's size and churn are consequences
    # of the module it covers, so ranking both double-counts the same decay.
    # With no line counts loaded there is nothing to filter against, so every
    # touched path is ranked rather than none.
    path_filter = (lambda p: p in line_counts) if line_counts else None
    candidates = rank_candidates(runs, line_counts=line_counts, path_filter=path_filter)
    return {
        "coverage": coverage,
        "controls": resolve_controls(runs),
        "runs": len(runs),
        "candidates": candidates,
        "threshold": threshold_status(coverage, candidates),
        "line_count_comparison": compare_to_line_counts(candidates, line_counts, top=top),
        "top": top,
    }


# ── Rendering ────────────────────────────────────────────────────────────


def _bound_line(coverage: dict) -> str | None:
    """Say which bound the denominator carries, or ``None`` when it carries none.

    Three distinguishable states, because naming the wrong one tells the operator
    to act on evidence that is not there: a capture era exists and bounds the
    denominator; a capture era exists but ``--since`` is the tighter bound; or no
    run joins a changed-file set at all, in which case there is no capture era to
    bound to and ``--since``, if given, is the only bound there is.
    """
    capture_start = coverage.get("capture_start_at")
    floor = coverage.get("coverage_floor")
    excluded = coverage.get("excluded_pre_capture_runs", 0)
    if capture_start is None:
        if floor is not None:
            return (
                "no run records a changed-file set, so there is no capture era to bound to; "
                f"the denominator is bounded only by --since (runs from {floor})"
            )
        if coverage.get("archive_runs"):
            return (
                "no run records a changed-file set, so there is no capture era to bound to; "
                f"the denominator is the whole archive ({coverage['archive_runs']} run(s))"
            )
        return None
    if floor != capture_start:
        # ``excluded`` counts what the capture bound removed beyond ``since``, which
        # is nothing when ``since`` is the tighter of the two — so do not report it.
        return (
            f"denominator bounded by --since (runs from {floor}), which is tighter than the "
            f"changed-file-capture era beginning {capture_start}"
        )
    return (
        f"denominator bounded to the changed-file-capture era (runs from {floor}); "
        f"{excluded} earlier cost-bearing run(s) excluded as unanalysable"
    )


def render(report: dict) -> str:
    """Render the report as the operator-facing text the spike record quotes."""
    coverage = report["coverage"]
    threshold = report["threshold"]
    out: list[str] = []
    out.append("STRUCTURAL DECAY OBSERVER — SPIKE POC (#2348)")
    out.append("")
    out.append("COVERAGE")
    out.append(
        f"  {coverage['joinable_runs']} of {coverage['measured_runs']} measured cost-bearing runs "
        f"in the analysed window join to a changed-file set ({coverage['run_coverage_ratio']:.1%})"
    )
    out.append(
        f"  ${coverage['joinable_spend_usd']:,.2f} of ${coverage['measured_spend_usd']:,.2f} "
        f"measured spend in the analysed window ({coverage['spend_coverage_ratio']:.1%})"
    )
    out.append(f"  window: {coverage['first_joinable_at']} .. {coverage['last_joinable_at']}")
    # State the bound rather than leaving the denominator's scope to be inferred
    # from a ratio (#2623).
    bound = _bound_line(coverage)
    if bound is not None:
        out.append(f"  {bound}")
    out.append("")
    out.append("CONTROLS")
    for control in report["controls"]:
        out.append(f"  {control.label:<40} {control.availability}")
    out.append("")
    out.append(f"TRUST THRESHOLD: {'MET' if threshold['met'] else 'NOT MET'}")
    for check in threshold["checks"]:
        mark = "ok  " if check["met"] else "FAIL"
        out.append(f"  [{mark}] {check['name']}: {check['detail']} (need {check['required']})")
        if not check["met"] and check.get("remedy"):
            out.append(f"         -> {check['remedy']}")
    if not threshold["met"]:
        out.append("")
        out.append(
            "  The ranking below is printed anyway so its shape can be inspected, but it is"
        )
        out.append("  NOT trustworthy at this sample size. Do not fund work from it.")
    out.append("")
    out.append(f"RANKING BY CONTROLLED EXCESS SPEND (top {report['top']})")
    candidates = report["candidates"][: report["top"]]
    if not candidates:
        out.append("  (no paths with a joinable changed-file set)")
    for i, candidate in enumerate(candidates, start=1):
        out.append("")
        out.append(f"  {i}. {candidate.path}")
        out.append(
            f"       {candidate.touching_runs} touching run(s), "
            f"${candidate.joinable_spend_usd:,.2f} attributed spend"
        )
        out.append(
            f"       excess: ${candidate.excess_usd:+,.2f} over "
            f"{candidate.controlled_comparisons} controlled comparison(s)"
        )
        if candidate.line_count is not None:
            out.append(f"       {candidate.line_count} lines")
        if candidate.co_touched:
            pairs = ", ".join(f"{p} ({n})" for p, n in candidate.co_touched)
            out.append(f"       co-touched with: {pairs}")
        if candidate.finding_mentions:
            out.append(f"       named in {candidate.finding_mentions} review finding(s)")
        out.append(f"       weakest signal: {candidate.weakest_signal}")
    out.append("")
    comparison = report["line_count_comparison"]
    out.append("COMPARISON AGAINST PURE LINE-COUNT RANKING (the ship gate)")
    out.append(f"  paths compared: {comparison['compared_paths']}")
    out.append(f"  spearman rank correlation: {comparison['spearman']}")
    out.append(
        f"  top-{report['top']} overlap: {len(comparison['overlap'])} "
        f"({comparison['overlap_ratio']:.0%})"
    )
    out.append(f"  by excess:     {', '.join(comparison['top_excess'][:5]) or '(none)'}")
    out.append(f"  by line count: {', '.join(comparison['top_line_count'][:5]) or '(none)'}")
    if comparison["biggest_movers"]:
        movers = ", ".join(f"{p} ({d:+d})" for p, d in comparison["biggest_movers"])
        out.append(f"  biggest rank movers: {movers}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-root", default=".", type=Path)
    parser.add_argument("--since", default=None, help="ISO date lower bound on run start")
    parser.add_argument("--top", default=15, type=int)
    args = parser.parse_args(argv)

    from theforge.coordinator.audit_storage import SubstrateError  # noqa: PLC0415

    try:
        report = load_report(args.project_root, since=args.since, top=args.top)
    except SubstrateError as exc:
        print(f"cannot read audit substrate: {exc}", file=sys.stderr)
        return 2
    print(render(report))
    return 0
