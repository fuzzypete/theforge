"""forge telemetry subcommand — per-phase cost/duration from the substrate."""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.coordinator import audit_substrate

_PHASE_NAMES = ["preflight", "plan", "plan_review", "dev", "validate", "review"]
_PHASE_LABELS = {
    "preflight": "PREFLIGHT",
    "plan": "PLAN",
    "plan_review": "PLAN_REVIEW",
    "dev": "DEV",
    "validate": "VALIDATE",
    "review": "REVIEW",
}


def cmd_telemetry(args: object) -> int:
    """Print per-phase cost/duration telemetry from history.jsonl."""
    # Locate project root
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print("Error: forge.yaml not found. Run from a forge project root.", file=sys.stderr)
        return 1
    project_root = config_path.parent

    # Parse --since date filter
    since_dt: datetime.datetime | None = None
    if args.since:
        try:
            since_dt = datetime.datetime.fromisoformat(args.since).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            print(f"Invalid --since date: {args.since!r} (expected YYYY-MM-DD)", file=sys.stderr)
            return 1

    phase_filter: str | None = getattr(args, "phase", None)

    try:
        conn = audit_substrate.require_substrate(project_root)
    except audit_substrate.SubstrateMissingError as exc:
        print(f"No audit history found: {exc}", file=sys.stderr)
        return 1
    except audit_substrate.SubstrateCorruptError as exc:
        print(f"Audit substrate is corrupt: {exc}", file=sys.stderr)
        return 1

    records: list[dict] = []
    try:
        for record in audit_substrate.iter_records(conn, order_by_started=True):
            if since_dt is not None:
                started = (record.get("timing") or {}).get("started_at")
                if started:
                    try:
                        ts = datetime.datetime.fromisoformat(started)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=datetime.timezone.utc)
                        if ts < since_dt:
                            continue
                    except ValueError:
                        pass
            records.append(record)
    finally:
        conn.close()

    if not records:
        print("No records found (try relaxing --since filter).")
        return 0

    # Last 10 by default
    recent = records[-10:]

    # ── Run table ─────────────────────────────────────────────────────
    _fmt_dur_s = lambda s: (  # noqa: E731
        f"{int(s // 60)}m{int(s % 60):02d}s" if s is not None else "—"
    )
    _fmt_cost_s = lambda c: f"${c:.2f}" if c is not None else "—"  # noqa: E731

    if not phase_filter:
        hdr = f"{'Run':<30} {'Cost':>8}  {'Time':>8}  {'Dev':>4}  {'Rev':>4}  {'Outcome'}"
        print(hdr)
        print("-" * len(hdr))
        for rec in recent:
            slug = (rec.get("task") or {}).get("slug", "?")
            slug_short = slug[:29]
            outcome = (rec.get("outcome") or {}).get("final_phase", "?").lower()
            totals = rec.get("totals") or {}
            cost = totals.get("cost_usd") or (rec.get("cost") or {}).get("total_usd")
            dur = totals.get("duration_s") or (rec.get("timing") or {}).get("duration_seconds")
            dev_iters = (
                totals.get("dev_iterations_productive")
                or totals.get("dev_iterations")
                or (rec.get("iterations") or {}).get("dev_iterations_productive")
                or (rec.get("iterations") or {}).get("dev_iterations", "?")
            )
            rev_cycles = (
                totals.get("review_cycles_total")
                or totals.get("review_cycles")
                or (rec.get("iterations") or {}).get("review_cycles_total")
                or (rec.get("iterations") or {}).get("review_cycles", "?")
            )
            print(
                f"{slug_short:<30} {_fmt_cost_s(cost):>8}  {_fmt_dur_s(dur):>8}  "
                f"{str(dev_iters):>4}  {str(rev_cycles):>4}  {outcome}"
            )
        print()

    # ── Phase breakdown ───────────────────────────────────────────────
    # Collect per-phase cost and duration across records that have phases block
    phase_costs: dict[str, list[float]] = {p: [] for p in _PHASE_NAMES}
    phase_durations: dict[str, list[float]] = {p: [] for p in _PHASE_NAMES}
    total_cost_all = 0.0
    records_with_phases = 0

    for rec in records:
        phases = rec.get("phases")
        if not phases:
            continue
        records_with_phases += 1
        rec_total = (rec.get("totals") or {}).get("cost_usd") or 0.0
        total_cost_all += rec_total
        for p in _PHASE_NAMES:
            pdata = phases.get(p)
            if not pdata:
                continue
            c = pdata.get("cost_usd")
            d = pdata.get("duration_s")
            if c is not None:
                phase_costs[p].append(c)
            if d is not None:
                phase_durations[p].append(d)

    phases_to_show = [phase_filter] if phase_filter else _PHASE_NAMES

    if records_with_phases == 0:
        print("No phase telemetry data found (runs predating this feature have no phases block).")
        return 0

    print(f"Phase breakdown (last {records_with_phases} run(s) with phase data):")
    avg_total = total_cost_all / records_with_phases if records_with_phases else 0.0
    for p in phases_to_show:
        if p not in _PHASE_NAMES:
            print(f"  Unknown phase: {p!r}", file=sys.stderr)
            continue
        label = _PHASE_LABELS[p]
        costs = phase_costs[p]
        durs = phase_durations[p]
        avg_cost = sum(costs) / len(costs) if costs else 0.0
        avg_dur = sum(durs) / len(durs) if durs else None
        pct = (avg_cost / avg_total * 100) if avg_total > 0 else 0.0
        pct_str = f"  ← {pct:.0f}% of cost" if pct >= 15 else ""
        dur_str = f"avg {_fmt_dur_s(avg_dur):>6}" if avg_dur is not None else "avg      —"
        print(f"  {label:<12} avg ${avg_cost:.2f}   {dur_str}{pct_str}")

    return 0


def register_parser(subparsers: object) -> None:
    """Register the 'telemetry' subcommand parser."""
    telemetry_parser = subparsers.add_parser(
        "telemetry", help="Show per-phase cost/duration telemetry across runs"
    )
    telemetry_parser.add_argument(
        "--since",
        metavar="DATE",
        help="Only include runs on or after this date (YYYY-MM-DD)",
    )
    telemetry_parser.add_argument(
        "--phase",
        metavar="PHASE",
        help="Show breakdown for a single phase (preflight|plan|plan_review|dev|validate|review)",
    )
    telemetry_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
