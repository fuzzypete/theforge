"""Substrate loading, rendering and CLI for the #1848 finding-fate spike."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from theforge.config.load import load_config
from theforge.coordinator.audit_read_model import iter_records
from theforge.coordinator.audit_storage import open_readonly
from theforge.reviewer_value import DEFAULT_VALUE_MIN_RUNS

from .analysis import analyze_records


def load_report(project_root: Path) -> dict:
    """Build the full report from the audit substrate. Opens read-only only."""
    project_root = Path(project_root).resolve()
    min_runs = DEFAULT_VALUE_MIN_RUNS
    recency = None
    config_path = project_root / "forge.yaml"
    if config_path.exists():
        try:
            cfg = load_config(config_path)
            recency = getattr(cfg.assignment, "recency", None)
            min_runs = int(getattr(cfg.assignment, "code_review_value_min_runs", min_runs))
        except Exception as exc:  # noqa: BLE001 - spike report should degrade, not abort
            print(f"warning: could not load config ({exc})", file=sys.stderr)

    conn = open_readonly(project_root)
    try:
        report = analyze_records(
            iter_records(conn, order_by_started=True),
            min_runs=min_runs,
            recency=recency,
        )
    finally:
        conn.close()
    report["project_root"] = str(project_root)
    return report


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def render(report: dict) -> str:
    """Render the operator-facing spike report."""
    corpus = report["corpus"]
    weighting = report["weighting"]
    lines = [
        "REVIEWER FINDING-FATE PROXY — SPIKE POC (#1848)",
        "",
        "CORPUS",
        f"  project root: {report.get('project_root', '.')}",
        f"  records scanned: {corpus['records_scanned']}",
        (
            "  records with code-review finding_registry: "
            f"{corpus['records_with_code_review_findings']}"
        ),
        (
            "  records with plan_finding_registry (ignored: no reporter): "
            f"{corpus['records_with_plan_review_findings']}"
        ),
        f"  admissible records: {corpus['admissible_records']}",
        f"  tainted records excluded: {corpus['tainted_records_excluded']}",
        f"  excluded P1 findings: {corpus['excluded_missing_attribution']} missing reporter, "
        f"{corpus['excluded_missing_fate']} no structured downstream fate",
        f"  adjacent markers: {corpus['gate_contradicted_markers']} gate_contradicted, "
        f"{corpus['downgraded_markers']} downgraded",
        "",
        "ADR-0006 GATES",
        f"  sample floor: {weighting['min_runs']} admissible review-run observations",
        (
            f"  recency: {weighting['mode']} "
            f"(half-life {weighting['half_life_runs']}, window {weighting['window']})"
        ),
        "  taint exclusion: applied at record/run level before aggregation",
        "",
        "FATE DETERMINATION",
    ]
    for name in ("addressed", "dismissed", "contradicted", "survived"):
        decision = report["fate_determination"][name]
        if decision["status"] == "derivable":
            lines.append(f"  {name}: derivable via {decision['derivation']}")
        else:
            lines.append(f"  {name}: UNDERIVABLE — needs {decision['missing_event']}")

    lines.extend(
        [
            "",
            "PER-REVIEWER RATES",
            (
                "  Rates are recency-weighted means of per-run fate shares; "
                "findings counts are reported separately."
            ),
            "",
            (
                "reviewer                findings derivable runs floor addressed "
                "dismissed contradicted survived  excl(tainted/downstream)"
            ),
        ]
    )
    if not report["reviewers"]:
        lines.append("(no attributable P1 findings in the admissible corpus)")
        return "\n".join(lines)

    name_width = max(22, max(len(str(row["reviewer"])) for row in report["reviewers"]))
    lines[-1] = (
        f"{'reviewer':<{name_width}} findings derivable runs floor "
        "addressed dismissed contradicted survived  excl(tainted/downstream)"
    )
    for row in report["reviewers"]:
        lines.append(
            f"{row['reviewer']:<{name_width}} "
            f"{row['findings']:>8} "
            f"{row['derivable_findings']:>9} "
            f"{row['runs']:>4} "
            f"{row['floor']:<4} "
            f"{_pct(row['addressed_rate']['rate']):>9} "
            f"{'n/a':>9} "
            f"{'n/a':>12} "
            f"{_pct(row['survived_rate']['rate']):>8}  "
            f"{row['tainted_runs_excluded']}/{row['excluded_missing_fate']}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-root", default=".", type=Path)
    args = parser.parse_args(argv)

    from theforge.coordinator.audit_storage import SubstrateError  # noqa: PLC0415

    try:
        report = load_report(args.project_root)
    except SubstrateError as exc:
        print(f"cannot read audit substrate: {exc}", file=sys.stderr)
        return 2
    print(render(report))
    return 0
