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
    print(f"forge audits: unknown subcommand {sub!r}", file=sys.stderr)
    return 2


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
        upsert_run_record,
    )

    summary = ImportSummary()
    if not history_path.exists():
        return summary
    conn = create_or_open(project_root)
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
