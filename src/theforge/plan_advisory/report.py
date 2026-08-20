"""Substrate loading and rendering for the plan-advisory resolution measure (#2112)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from theforge.coordinator.audit_read_model import iter_records
from theforge.coordinator.audit_storage import open_readonly

from .analysis import (
    EVIDENCE_UNAVAILABLE,
    CorpusMismatchError,
    analyze,
    extract_plan_findings,
)

JUDGMENTS_PATH = Path(__file__).with_name("judgments.json")


def load_judgments(path: Path | None = None) -> dict[str, Any]:
    """Read the checked-in judgment corpus.

    Raises ``OSError`` when the file is unreadable and ``ValueError`` (including
    ``json.JSONDecodeError``) when its contents are not a corpus. Callers that
    face an operator convert both — see :func:`_read_corpus`.
    """
    source = Path(path) if path is not None else JUDGMENTS_PATH
    with open(source, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict) or not isinstance(payload.get("judgments"), list):
        raise ValueError(f"{source}: expected an object with a 'judgments' list")
    return payload


def _read_corpus(judgments_path: Path | None) -> dict[str, Any]:
    """Read the corpus, reporting an unusable file the way a mismatched one is.

    An operator cannot act differently on "the corpus disagrees with the
    substrate" and "the corpus could not be read at all" — both mean the rate is
    not computable and the file is what to look at. Converting here keeps that
    single failure mode at one seam, so every entry point handles it without its
    own ``except`` clause.
    """
    try:
        return load_judgments(judgments_path)
    except (OSError, ValueError) as exc:
        source = judgments_path or JUDGMENTS_PATH
        raise CorpusMismatchError(f"judgment corpus at {source} is unusable: {exc}") from exc


def load_report(project_root: Path, *, judgments_path: Path | None = None) -> dict[str, Any]:
    """Build the report, reading the audit substrate read-only.

    Raises :class:`~theforge.plan_advisory.analysis.CorpusMismatchError` when the
    judgment corpus cannot be read or does not line up with the substrate, and
    ``SubstrateError`` when the substrate itself is missing or corrupt.
    """
    project_root = Path(project_root).resolve()
    conn = open_readonly(project_root)
    try:
        extraction = extract_plan_findings(iter_records(conn, order_by_started=True))
    finally:
        conn.close()
    payload = _read_corpus(judgments_path)
    report = analyze(extraction, payload["judgments"])
    report["project_root"] = str(project_root)
    report["corpus"]["judgment_source"] = str(judgments_path or JUDGMENTS_PATH)
    report["corpus"]["judgment_notes"] = payload.get("notes")
    return report


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _wrap(text: str, width: int, indent: str, *, label: str = "") -> list[str]:
    """Word-wrap ``text`` at ``width``, prefixing the first line with ``label``.

    Continuation lines get ``indent``; the label is padded to the same width so
    the block stays flush under its first line.
    """
    words = str(text).split()
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    if not label:
        return [indent + chunk for chunk in chunks]
    head = f"{indent}{label}"
    return [head + chunks[0]] + [" " * len(head) + chunk for chunk in chunks[1:]]


def _finding_lines(row: dict[str, Any], *, verbose: bool) -> list[str]:
    head = (
        f"  {row['slug']:<12} {row['class']:<26} "
        f"{'shipped-addressed' if row['shipped_addressed'] else 'not addressed'}"
    )
    point = row.get("detection_point")
    if point == "unshipped":
        head += "  -> never caught (still latent)"
    elif point:
        head += f"  -> caught at {point}"
    lines = [head]
    if verbose:
        lines.extend(_wrap(row["description"], 84, "        ", label="finding:  "))
        lines.extend(_wrap(row["evidence"], 84, "        ", label="evidence: "))
    return lines


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Render the operator-facing decision report."""
    corpus = report["corpus"]
    overall = report["overall"]
    cost = report["cost"]

    lines = [
        "PLAN ADVISORY RESOLUTION (#2112)",
        "",
        (
            f"Plan advisory resolution — {corpus['runs']} runs, "
            f"{corpus['findings_judged']} judged findings "
            f"of {corpus['findings_extracted']} extracted "
            f"(coverage {_pct(corpus['coverage'])})"
        ),
        f"  project root: {report.get('project_root', '.')}",
        f"  audit records scanned: {corpus['records_scanned']}",
        ("  corpus: completed (DONE) runs carrying a plan review with P1-level plan findings"),
        (
            f"  excluded by final_phase: {corpus['excluded_run_count']} run(s) "
            f"carrying {corpus['excluded_run_findings']} P1-level finding(s) "
            "(reached dev but did not ship)"
        ),
        f"  P2 plan findings out of scope: {corpus['p2_findings_skipped']}",
        (
            f"  P1-level findings raised: {corpus['p1_findings_raised']}; "
            f"{corpus['findings_fixed_in_plan']} resolved by plan regeneration "
            "before dev (not advisory, excluded from the rate)"
        ),
        (f"  unjudged findings excluded from every rate: {corpus['findings_unjudged']}"),
        (f"  judgments carrying no citable evidence: {corpus['evidence_unavailable']}"),
        "",
        (
            f"overall: {overall['resolved']}/{overall['findings']} advisory findings "
            f"resolved ({_pct(overall['rate'])}); "
            f"{overall['shipped_addressed']} shipped-addressed, "
            f"{overall['shipped_unaddressed']} not"
        ),
        "",
        f"{'class':<28}{'findings':>9}{'resolved':>10}{'escaped':>9}{'rate':>7}",
        f"{'-' * 28}{'-' * 9:>9}{'-' * 10:>10}{'-' * 9:>9}{'-' * 7:>7}",
    ]
    for row in report["classes"]:
        lines.append(
            f"{row['class']:<28}{row['findings']:>9}{row['resolved']:>10}"
            f"{row['escaped']:>9}{_pct(row['rate']):>7}"
        )
    if not report["classes"]:
        lines.append("(no judged findings)")

    lines.extend(
        [
            "",
            (
                f"plan review cost:  median {_usd(cost['median_plan_review_usd'])}/story "
                f"(n={cost['runs_with_plan_review_cost']})   "
                f"median {_pct(cost['median_fraction_of_story'])} of story cost "
                f"(n={cost['runs_with_both']})"
            ),
            (
                f"  of {cost['runs']} corpus run(s): "
                f"{cost['omitted_missing_plan_review_cost']} record no plan-review "
                f"cost, {cost['omitted_missing_total_cost']} no story total — "
                "both omitted rather than imputed"
            ),
            "",
            "escapes by later detection point:",
        ]
    )
    points = report["escapes_by_detection_point"]
    if points:
        lines.append("  " + " · ".join(f"{name} {count}" for name, count in points.items()))
    else:
        lines.append("  (none)")

    lines.extend(["", f"escaped findings ({len(report['escaped_findings'])}):"])
    for row in report["escaped_findings"]:
        lines.extend(_finding_lines(row, verbose=verbose))
    if not report["escaped_findings"]:
        lines.append("  (none)")

    lines.extend(["", f"resolved findings ({len(report['resolved_findings'])}):"])
    for row in report["resolved_findings"]:
        lines.extend(_finding_lines(row, verbose=verbose))
    if not report["resolved_findings"]:
        lines.append("  (none)")

    if corpus["findings_unjudged"]:
        lines.extend(["", f"unjudged findings ({corpus['findings_unjudged']}):"])
        for row in report["unjudged_findings"]:
            lines.append(f"  {row['slug']:<12} {row['finding_key']}")

    if corpus.get("judgment_notes"):
        lines.extend(["", "corpus notes:", *_wrap(corpus["judgment_notes"], 86, "  ")])
    if corpus["evidence_unavailable"]:
        lines.append(
            f"  NOTE: {corpus['evidence_unavailable']} judgment(s) marked "
            f"'{EVIDENCE_UNAVAILABLE}' — treat their rows as weaker evidence."
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-root", default=".", type=Path)
    parser.add_argument("--judgments", default=None, type=Path)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit the raw report as JSON")
    args = parser.parse_args(argv)

    from theforge.coordinator.audit_storage import SubstrateError  # noqa: PLC0415

    try:
        report = load_report(args.project_root, judgments_path=args.judgments)
    except SubstrateError as exc:
        print(f"cannot read audit substrate: {exc}", file=sys.stderr)
        return 2
    except CorpusMismatchError as exc:
        print(f"judgment corpus unusable: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report, verbose=args.verbose))
    return 0
