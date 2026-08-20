"""forge audits subcommand group — manage the SQLite audit substrate."""

from __future__ import annotations

import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.coordinator import audit_substrate


def cmd_audits(args: object) -> int:
    """Dispatch ``forge audits <subcommand>``."""
    sub = getattr(args, "audits_command", None)
    if sub == "rebuild":
        return _cmd_audits_rebuild(args)
    if sub == "export-assignment-history":
        return _cmd_audits_export_assignment_history(args)
    if sub == "show":
        return _cmd_audits_show(args)
    if sub == "skips":
        return _cmd_audits_skips(args)
    if sub == "alias-drift":
        return _cmd_audits_alias_drift(args)
    if sub == "plan-advisory":
        return _cmd_audits_plan_advisory(args)
    print(f"forge audits: unknown subcommand {sub!r}", file=sys.stderr)
    return 2


def _cmd_audits_plan_advisory(args: object) -> int:
    """Report how often dev resolved an advisory plan-review finding (issue #2112).

    Plan review approves plans while still holding P1-level findings and hands
    them to dev as advisory context. This joins those findings to a checked-in
    judgment corpus and renders the resolution rate by finding class, the
    enumerable resolved and escaped findings, and plan-review cost as a fraction
    of the story it guarded.

    Thin by construction (CLI CONVENTIONS): loading, joining and rendering all
    live in ``theforge.plan_advisory``; this resolves the project root and prints.
    """
    from theforge.plan_advisory.analysis import CorpusMismatchError  # noqa: PLC0415
    from theforge.plan_advisory.report import load_report, render  # noqa: PLC0415

    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print("[forge] forge.yaml not found. Run from a forge project root.", file=sys.stderr)
        return 1
    try:
        report = load_report(config_path.parent)
    except audit_substrate.SubstrateError as exc:
        print(f"[forge] {exc}", file=sys.stderr)
        return 1
    except CorpusMismatchError as exc:
        print(
            f"[forge] the plan-advisory judgment corpus does not match the audit substrate: {exc}",
            file=sys.stderr,
        )
        return 1
    print(render(report, verbose=bool(getattr(args, "verbose", False))))
    return 0


def _cmd_audits_alias_drift(args: object) -> int:
    """Report what each configured model identity resolved to (issue #2226).

    A family alias (``anthropic/opus/cli``) names whichever version the vendor
    currently ships, so the identity recorded against a run does not say what
    ran. This reads the substrate's per-invocation identity index and reports,
    per configured identity, the ordered set of concrete identities it resolved
    to. An alias whose resolution changed shows more than one — which is the
    point: the change becomes something an operator can query rather than
    something they infer from a behavioural surprise.

    Thin by construction (CLI CONVENTIONS): the grouping lives in
    ``audit_substrate.alias_resolution_timeline``; this renders it.
    """
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print("[forge] forge.yaml not found. Run from a forge project root.", file=sys.stderr)
        return 1
    project_root = config_path.parent
    try:
        conn = audit_substrate.require_substrate(project_root)
    except audit_substrate.SubstrateError as exc:
        print(f"[forge] {exc}", file=sys.stderr)
        return 1
    try:
        timeline = audit_substrate.alias_resolution_timeline(conn)
    finally:
        conn.close()

    if getattr(args, "changed_only", False):
        timeline = [entry for entry in timeline if entry["changed"]]
    if not timeline:
        print("[forge] no recorded invocation carries both a configured and a resolved identity.")
        return 0

    for entry in timeline:
        marker = "CHANGED" if entry["changed"] else "stable"
        print(
            f"{entry['configured_model']}  "
            f"[{marker}: {entry['distinct_resolved']} resolved identit"
            f"{'ies' if entry['distinct_resolved'] != 1 else 'y'} over "
            f"{entry['invocations']} invocation(s)]"
        )
        for resolved in entry["resolved_models"]:
            window = resolved["first_seen"] or "?"
            if resolved["last_seen"] and resolved["last_seen"] != resolved["first_seen"]:
                window = f"{window} → {resolved['last_seen']}"
            print(
                f"    {resolved['resolved_model']:<40} "
                f"{resolved['invocations']:>4} invocation(s)  "
                f"[{resolved['resolution'] or '?'}]  {window}"
            )
        print()
    return 0


def _cmd_audits_skips(args: object) -> int:
    """Query shape-gate skip events from the audit substrate (issue #1453).

    Answers "show me all sprints in date range D where skip code C fired" — the
    question that took manual log-walking this week — as a one-line query. With
    ``--stuck`` it instead lists repeated-block patterns: issues blocked by the
    same code ``>= threshold`` times across runs.
    """
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print(
            "[forge] forge.yaml not found. Run from a forge project root.",
            file=sys.stderr,
        )
        return 1
    project_root = config_path.parent
    sub_path = audit_substrate.substrate_path(project_root)
    if not sub_path.exists():
        print(
            f"[forge] audit substrate not found at {sub_path}. "
            f"Run `forge audits rebuild` to create it.",
            file=sys.stderr,
        )
        return 1

    conn = audit_substrate.create_or_open(project_root)
    try:
        if getattr(args, "stuck", False):
            threshold = int(getattr(args, "threshold", None) or 3)
            rows = audit_substrate.repeated_shape_skip_blocks(
                conn,
                threshold=threshold,
                since=getattr(args, "since", None),
                until=getattr(args, "until", None),
            )
            if not rows:
                print(f"[forge] no issues blocked >= {threshold} time(s) by the same skip code.")
                return 0
            header = ("issue", "code", "blocks", "first_seen", "last_seen", "runs")
            fmt_rows: list[tuple[str, ...]] = [header]
            for r in rows:
                fmt_rows.append(
                    (
                        f"#{r['issue_id']}",
                        _fmt(r["reason_code"]),
                        str(r["block_count"]),
                        _fmt((r.get("first_seen") or "")[:19]),
                        _fmt((r.get("last_seen") or "")[:19]),
                        str(len(r.get("run_ids") or [])),
                    )
                )
            _print_table(fmt_rows)
            print(f"\n{len(rows)} stuck pattern(s) (threshold={threshold})")
            return 0

        events = list(
            audit_substrate.iter_shape_skip_events(
                conn,
                issue_id=getattr(args, "issue", None),
                reason_code=getattr(args, "code", None),
                category=getattr(args, "category", None),
                since=getattr(args, "since", None),
                until=getattr(args, "until", None),
            )
        )
    finally:
        conn.close()

    if not events:
        print("[forge] no shape-gate skip events matched.")
        return 0

    header = ("issue", "code", "category", "severity", "source", "prior", "emitted_at")
    fmt_rows = [header]
    for e in events:
        fmt_rows.append(
            (
                f"#{e.get('issue_id')}",
                _fmt(e.get("reason_code")),
                _fmt(e.get("category")),
                _fmt(e.get("severity")),
                _fmt(e.get("source")),
                str(e.get("prior_block_count", 0)),
                _fmt(str(e.get("emitted_at") or "")[:19]),
            )
        )
    _print_table(fmt_rows)
    print(f"\n{len(events)} skip event(s) shown")
    return 0


def _print_table(fmt_rows: list[tuple[str, ...]]) -> None:
    """Render header + separator + rows as an aligned text table."""
    if not fmt_rows:
        return
    ncols = len(fmt_rows[0])
    widths = [max(len(r[i]) for r in fmt_rows) for i in range(ncols)]
    for i, row in enumerate(fmt_rows):
        print("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)))
        if i == 0:
            print("  ".join("-" * w for w in widths))


def _cmd_audits_show(args: object) -> int:
    """Render rows from the SQLite audit substrate.

    Provides an operator-visible surface for substrate rows that have no
    corresponding per-run YAML — in particular legacy rows imported from
    ``history.jsonl`` (provenance ``legacy_history_jsonl``), which
    ``forge audit <file>`` cannot address.
    """
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print(
            "[forge] forge.yaml not found. Run from a forge project root.",
            file=sys.stderr,
        )
        return 1
    project_root = config_path.parent
    sub_path = audit_substrate.substrate_path(project_root)
    if not sub_path.exists():
        print(
            f"[forge] audit substrate not found at {sub_path}. "
            f"Run `forge audits rebuild` to create it.",
            file=sys.stderr,
        )
        return 1

    slug = getattr(args, "slug", None)
    raw_limit = getattr(args, "limit", None)
    limit = int(raw_limit) if raw_limit is not None else 20
    if limit <= 0:
        print("[forge] --limit must be a positive integer.", file=sys.stderr)
        return 2

    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(sub_path))
    try:
        sql = (
            "SELECT slug, started_at, final_phase, complexity_score, "
            "provenance, landing_status, total_cost_usd "
            "FROM audit_records"
        )
        params: tuple
        if slug:
            sql += " WHERE slug = ?"
            params = (slug,)
        else:
            params = ()
        sql += " ORDER BY COALESCE(started_at, '') DESC LIMIT ?"
        params = params + (limit,)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not rows:
        if slug:
            print(f"[forge] no audit records found for slug {slug!r}.")
        else:
            print("[forge] no audit records found.")
        return 0

    header = ("slug", "started_at", "final_phase", "score", "provenance", "landing", "cost_usd")
    fmt_rows: list[tuple[str, ...]] = [header]
    for slug_, started, phase, score, prov, landing, cost in rows:
        fmt_rows.append(
            (
                _fmt(slug_),
                _fmt(started),
                _fmt(phase),
                _fmt(score),
                _fmt(prov),
                _fmt(landing),
                f"{cost:.4f}" if isinstance(cost, (int, float)) else _fmt(cost),
            )
        )
    widths = [max(len(r[i]) for r in fmt_rows) for i in range(len(header))]
    for i, row in enumerate(fmt_rows):
        print("  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)))
        if i == 0:
            print("  ".join("-" * w for w in widths))
    suffix = f", slug={slug}" if slug else ""
    print(f"\n{len(rows)} record(s) shown (limit={limit}{suffix})")
    return 0


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    return str(value)


def _cmd_audits_export_assignment_history(args: object) -> int:
    """Write a human-readable assignment_history.yaml from the audit substrate.

    The on-disk snapshot is purely an inspection convenience; the
    coordinator reads adaptive history straight from the substrate, so
    operators may delete the file at any time.
    """
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print(
            "[forge] forge.yaml not found. Run from a forge project root.",
            file=sys.stderr,
        )
        return 1
    project_root = config_path.parent
    if not audit_substrate.has_audit_inputs(project_root):
        print(
            "[forge] no audit records found — nothing to export.",
            file=sys.stderr,
        )
        return 1
    try:
        conn = audit_substrate.require_substrate(project_root)
    except audit_substrate.SubstrateError as exc:
        print(f"[forge] {exc}", file=sys.stderr)
        return 1
    try:
        dicts = audit_substrate.derive_assignment_history(conn)
    finally:
        conn.close()

    output = (
        Path(args.output).resolve()
        if getattr(args, "output", None)
        else (project_root / ".forge" / "assignment_history.yaml")
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    import yaml as _yaml

    payload = {"escalations": [_strip_none_score(d) for d in dicts]}
    with open(output, "w", encoding="utf-8") as fh:
        _yaml.dump(payload, fh, default_flow_style=False, allow_unicode=True, sort_keys=True)
    print(f"[forge] wrote {len(dicts)} records to {output}")
    return 0


def _strip_none_score(record: dict) -> dict:
    """Drop ``complexity_score`` when null so the YAML matches legacy shape."""
    out = dict(record)
    if out.get("complexity_score") is None:
        out.pop("complexity_score", None)
    return out


def _cmd_audits_rebuild(args: object) -> int:
    """Regenerate the audit substrate from per-run JSON files.

    With ``--include-legacy-history``, additionally backfill from
    ``history.jsonl``. The legacy-history path is reconciled by stable
    identity so reruns repair previously-imported rows rather than
    duplicating them: prior legacy rows are snapshotted before the
    destructive rebuild and re-applied to compute correct
    imported / skipped_existing / updated_repaired counts.
    """
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print(
            "[forge] forge.yaml not found. Run from a forge project root.",
            file=sys.stderr,
        )
        return 1
    project_root = config_path.parent
    include_legacy = bool(getattr(args, "include_legacy_history", False))

    # Snapshot prior legacy rows BEFORE the destructive rebuild so that
    # a `--include-legacy-history` rerun reports skipped_existing /
    # updated_repaired correctly (the plan reviewer P1 notes this).
    legacy_snapshot: dict[str, str] = {}
    sub_path = audit_substrate.substrate_path(project_root)
    if include_legacy and sub_path.exists():
        try:
            import sqlite3 as _sqlite3

            _conn = _sqlite3.connect(str(sub_path))
            try:
                for run_id, raw_json in _conn.execute(
                    "SELECT run_id, raw_json FROM audit_records "
                    "WHERE provenance = 'legacy_history_jsonl'"
                ):
                    legacy_snapshot[run_id] = raw_json
            finally:
                _conn.close()
        except Exception:  # noqa: BLE001
            legacy_snapshot = {}

    rebuild_summary = audit_substrate.rebuild_from_runs(project_root)

    history_path = audit_substrate.history_jsonl_path(project_root)
    auto_legacy = not include_legacy and history_path.exists() and rebuild_summary.imported == 0
    import_summary = None
    if include_legacy or auto_legacy:
        if include_legacy and legacy_snapshot:
            print("[forge] migrating history.jsonl into audit substrate (repair-safe)")
            import_summary = _import_with_snapshot(project_root, history_path, legacy_snapshot)
        else:
            print("[forge] migrating history.jsonl into audit substrate (one-shot)")
            import_summary = audit_substrate.import_history_jsonl(project_root)

    print(
        f"[forge] rebuild scanned {rebuild_summary.runs_seen} run files: "
        f"imported {rebuild_summary.imported}, failed {rebuild_summary.failed}"
    )
    if import_summary is not None:
        print(
            f"[forge] imported {import_summary.imported}, "
            f"skipped existing {import_summary.skipped_existing}, "
            f"updated/repaired {import_summary.updated_repaired}, "
            f"failed {import_summary.failed}"
        )
    print(f"[forge] substrate ready at {audit_substrate.substrate_path(project_root)}")

    if rebuild_summary.failed or (import_summary and import_summary.failed):
        return 1
    return 0


def _import_with_snapshot(
    project_root: Path,
    history_path: Path,
    snapshot: dict[str, str],
) -> "audit_substrate.ImportSummary":
    """Import legacy history while reconciling against a pre-rebuild snapshot.

    Without the snapshot, every legacy row would land as ``imported`` after
    a destructive rebuild because the substrate has no prior legacy rows
    to compare against. This walks the history.jsonl file ourselves so we
    can classify each record against the snapshot's stable identity.
    """
    from theforge.coordinator.audit_substrate import (
        ImportSummary,
        _canonical_json,
        create_or_open,
        derive_run_id,
        secrets_env_path,
        upsert_run_record,
    )

    summary = ImportSummary()
    if not history_path.exists():
        return summary
    conn = create_or_open(project_root)
    env_file = secrets_env_path(project_root)
    env_file_arg: Path | None = env_file if env_file.exists() else None
    try:
        try:
            with open(history_path, encoding="utf-8") as fh:
                import json as _json

                for lineno, raw in enumerate(fh, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = _json.loads(raw)
                    except _json.JSONDecodeError as exc:
                        summary.failed += 1
                        summary.failures.append(f"line {lineno}: {exc}")
                        continue
                    if not isinstance(record, dict):
                        summary.failed += 1
                        summary.failures.append(f"line {lineno}: not an object")
                        continue
                    run_id = derive_run_id(record)
                    new_raw = _canonical_json(record)
                    prev_raw = snapshot.get(run_id)
                    result = upsert_run_record(
                        conn,
                        record,
                        provenance="legacy_history_jsonl",
                        source_path=history_path.name,
                        env_file=env_file_arg,
                    )
                    # A native row with the same identity is canonical and
                    # protected; count those as skipped-existing.
                    if result.skipped_protected:
                        summary.skipped_existing += 1
                    elif prev_raw is None:
                        summary.imported += 1
                    elif prev_raw == new_raw:
                        summary.skipped_existing += 1
                    else:
                        summary.updated_repaired += 1
        except OSError as exc:
            summary.failed += 1
            summary.failures.append(f"open: {exc}")
        from theforge.coordinator.audit_substrate import _meta_set

        _meta_set(conn, "legacy_import_done", "1")
        conn.commit()
    finally:
        conn.close()
    return summary


def register_parser(subparsers: object) -> None:
    """Register the ``audits`` subcommand group."""
    audits_parser = subparsers.add_parser("audits", help="Manage the SQLite audit substrate")
    audits_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
    audits_sub = audits_parser.add_subparsers(dest="audits_command", required=True)

    rebuild_parser = audits_sub.add_parser(
        "rebuild", help="Rebuild the audit substrate from per-run JSON files"
    )
    rebuild_parser.add_argument(
        "--include-legacy-history",
        action="store_true",
        help="Also backfill from .forge/audits/history.jsonl",
    )
    rebuild_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )

    export_parser = audits_sub.add_parser(
        "export-assignment-history",
        help="Write a human-readable assignment_history.yaml from the substrate",
    )
    export_parser.add_argument(
        "--output",
        help="Output path (default: .forge/assignment_history.yaml)",
    )
    export_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )

    alias_drift_parser = audits_sub.add_parser(
        "alias-drift",
        help="Report what each configured model identity resolved to across recorded runs",
    )
    alias_drift_parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Only show configured identities that resolved to more than one model",
    )
    alias_drift_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )

    plan_advisory_parser = audits_sub.add_parser(
        "plan-advisory",
        help="Report how often dev resolved an advisory plan-review finding",
    )
    plan_advisory_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include each finding's text and the evidence its judgment cites",
    )
    plan_advisory_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )

    show_parser = audits_sub.add_parser(
        "show",
        help="Render rows from the SQLite audit substrate (incl. imported legacy rows)",
    )
    show_parser.add_argument(
        "--slug",
        help="Filter to a single slug (e.g. issue-1325)",
    )
    show_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to render (default: 20)",
    )
    show_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )

    skips_parser = audits_sub.add_parser(
        "skips",
        help="Query shape-gate skip events (by code, issue, date range) or stuck patterns",
    )
    skips_parser.add_argument("--code", help="Filter to a single skip reason code")
    skips_parser.add_argument("--issue", help="Filter to a single issue id (e.g. 1135)")
    skips_parser.add_argument("--category", help="Filter to a taxonomy category")
    skips_parser.add_argument(
        "--since", help="Only events with emitted_at >= this ISO-8601 timestamp"
    )
    skips_parser.add_argument(
        "--until", help="Only events with emitted_at <= this ISO-8601 timestamp"
    )
    skips_parser.add_argument(
        "--stuck",
        action="store_true",
        help="List repeated-block patterns (issues blocked by the same code >= threshold times)",
    )
    skips_parser.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="Stuck-pattern threshold when --stuck is set (default: 3)",
    )
    skips_parser.add_argument(
        "--config",
        help="Path to forge.yaml (default: auto-detect)",
    )
