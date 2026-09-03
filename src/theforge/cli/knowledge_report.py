"""``forge knowledge-report`` — is the knowledge feed-forward loop earning its keep?

Thin command layer over :mod:`theforge.knowledge_effectiveness`: resolve config,
open the audit substrate, bound the record window, hand the records to the pure
report, render it. Every judgment — cohort classification, comparability
bucketing, the insufficient-data threshold — lives in the core module so the
terminal view and the structured payload cannot diverge from what the tests
assert.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.coordinator import audit_read_model, audit_storage


def cmd_knowledge_report(args: argparse.Namespace) -> int:
    """Render the knowledge-effectiveness report over a bounded run window."""
    import yaml  # noqa: PLC0415

    from theforge.knowledge_effectiveness import build_report  # noqa: PLC0415
    from theforge.knowledge_effectiveness_render import (  # noqa: PLC0415
        render_terminal,
        report_payload,
    )
    from theforge.knowledge_invariant_proof import (  # noqa: PLC0415
        build_invariant_proof_from_records,
    )
    from theforge.knowledge_receipts_report import (  # noqa: PLC0415
        build_receipt_distribution,
    )
    from theforge.knowledge_receipts_report import (
        render_terminal as render_receipts,  # noqa: PLC0415
    )
    from theforge.knowledge_receipts_report import (
        report_payload as receipt_payload,  # noqa: PLC0415
    )

    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1
    project_root = config_path.parent

    try:
        since = _parse_bound(args.since, "--since")
        until = _parse_bound(args.until, "--until", end_of_day=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    recent_run_count: int | None = args.recent_run_count
    if recent_run_count is not None and recent_run_count < 1:
        print("--recent-run-count must be at least 1", file=sys.stderr)
        return 1

    try:
        records = _load_records(
            project_root, since=since, until=until, recent_run_count=recent_run_count
        )
    except audit_storage.SubstrateMissingError as exc:
        print(f"No audit history found: {exc}", file=sys.stderr)
        return 1
    except audit_storage.SubstrateCorruptError as exc:
        print(f"Audit substrate is corrupt: {exc}", file=sys.stderr)
        return 1

    report = build_report(
        records,
        since=args.since,
        until=args.until,
        recent_run_count=recent_run_count,
    )

    proof = build_invariant_proof_from_records(records)
    uptake = _uptake_sections(records)
    # Prior-run knowledge receipts (#2866). Its own section, its own totals: a
    # receipt count is not a contribution to the cohort effectiveness verdict and
    # must not be readable as one.
    receipts = build_receipt_distribution(records)

    if args.format == "terminal":
        print(render_terminal(report, proof), end="")
        print(_render_uptake_sections(uptake), end="")
        print(render_receipts(receipts), end="")
        return 0

    payload = report_payload(report, proof)
    payload["prior_run_uptake"] = uptake
    payload["knowledge_receipts"] = receipt_payload(receipts)
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), end="")
    return 0


def _uptake_sections(records: list[dict]) -> list[dict]:
    """One prior-run uptake block per run in the window, newest last.

    Kept out of the cohort report proper on purpose: the cohort metrics are an
    effectiveness question and this is not one. A reader must not be able to
    mistake a correspondence count for a contribution to that verdict (#2684).
    """
    sections = []
    for record in records:
        uptake = record.get("prior_run_uptake")
        if isinstance(uptake, dict):
            sections.append({"run_id": record.get("run_id"), **uptake})
    return sections


def _render_uptake_sections(sections: list[dict]) -> str:
    from theforge.knowledge_uptake_render import render_run_uptake  # noqa: PLC0415

    if not sections:
        return ""
    blocks = ["\n"]
    for section in sections:
        run_id = section.get("run_id") or "unknown run"
        blocks.append(f"── run {run_id} ──\n")
        blocks.append(render_run_uptake(section))
        blocks.append("\n")
    return "".join(blocks)


def _load_records(
    project_root: Path,
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
    recent_run_count: int | None,
) -> list[dict]:
    """Read the bounded record window from the substrate.

    ``--recent-run-count`` bounds by position and the date flags bound by time;
    when both are given the date filter is applied first and the count then
    takes the most recent survivors, so the two never fight over which window
    won.
    """
    conn = audit_storage.require_substrate(project_root)
    try:
        records = [
            record
            for record in audit_read_model.iter_records(conn, order_by_started=True)
            if _in_window(record, since=since, until=until)
        ]
    finally:
        conn.close()
    if recent_run_count is not None:
        return records[-recent_run_count:]
    return records


def _in_window(
    record: dict,
    *,
    since: datetime.datetime | None,
    until: datetime.datetime | None,
) -> bool:
    """A record with no parseable start time is kept — bounds exclude, not guess."""
    if since is None and until is None:
        return True
    timing = record.get("timing")
    started = timing.get("started_at") if isinstance(timing, dict) else None
    if not isinstance(started, str) or not started:
        return True
    try:
        stamp = datetime.datetime.fromisoformat(started)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    if since is not None and stamp < since:
        return False
    return not (until is not None and stamp > until)


def _parse_bound(
    value: str | None, flag: str, *, end_of_day: bool = False
) -> datetime.datetime | None:
    """Parse a window bound, treating a bare ``--until`` date as inclusive.

    ``--until 2026-08-04`` means "through the 4th": parsed as midnight it would
    silently drop every run that day, which reads as data loss rather than as a
    bound. A bound given with an explicit time is honoured as written.
    """
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {flag} value: {value!r} (expected YYYY-MM-DD or ISO-8601 timestamp)"
        ) from exc
    if end_of_day and len(value.strip()) == len("YYYY-MM-DD"):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def register_parser(subparsers: object) -> None:
    """Register the ``knowledge-report`` subcommand parser."""
    p = subparsers.add_parser(
        "knowledge-report",
        help=(
            "Knowledge-loop effectiveness: do stories that received prior run "
            "summaries regenerate plans, repeat review findings, iterate, and "
            "cost less than comparable stories that did not"
        ),
    )
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument(
        "--since",
        metavar="DATE",
        help="Only include runs started on or after this date (YYYY-MM-DD or ISO-8601)",
    )
    p.add_argument(
        "--until",
        metavar="DATE",
        help="Only include runs started on or before this date (YYYY-MM-DD or ISO-8601)",
    )
    p.add_argument(
        "--recent-run-count",
        type=int,
        metavar="N",
        help="Only include the most recent N runs in the window (default: all)",
    )
    p.add_argument(
        "--format",
        choices=("terminal", "yaml", "json"),
        default="terminal",
        help="Output format (default: terminal)",
    )
