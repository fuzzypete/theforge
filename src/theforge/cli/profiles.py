"""``forge profiles`` — inspect and reset model capability profile history."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

from theforge.config import is_canonical_model_id
from theforge.model_profiles import (
    COMPLEXITY_BANDS,
    ROLES,
    canonical_id_for_legacy_key,
    load_profiles,
    record_profile_reset,
)


def register_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "profiles",
        help="Inspect and maintain model capability profiles",
    )
    profile_subparsers = parser.add_subparsers(dest="profiles_command", required=True)

    list_parser = profile_subparsers.add_parser("list", help="List current profile counters")
    list_parser.add_argument(
        "--project-root",
        default=".",
        help="Project root containing the .forge directory (default: cwd)",
    )
    list_parser.add_argument(
        "--model",
        "--filter",
        dest="model",
        default=None,
        help="Show only one canonical model ID",
    )
    list_parser.add_argument(
        "--role",
        choices=ROLES,
        default=None,
        help="Show only one role",
    )

    reset_parser = profile_subparsers.add_parser(
        "reset",
        help="Reset cumulative profile counters for one canonical model ID",
    )
    reset_parser.add_argument(
        "--project-root",
        default=".",
        help="Project root containing the .forge directory (default: cwd)",
    )
    reset_parser.add_argument(
        "--model",
        required=True,
        help="Canonical model ID to reset (for example anthropic/sonnet/cli)",
    )
    reset_parser.add_argument(
        "--role",
        choices=ROLES,
        default=None,
        help="Reset only one role's history",
    )
    reset_parser.add_argument(
        "--complexity",
        choices=COMPLEXITY_BANDS,
        default=None,
        help="Reset only one dev complexity bucket",
    )
    reset_parser.add_argument(
        "--reason",
        default=None,
        help="Optional operator-supplied reason to record in the reset audit log",
    )


def cmd_profiles(args: argparse.Namespace) -> int:
    if args.profiles_command == "list":
        return cmd_profiles_list(args)
    if args.profiles_command == "reset":
        return cmd_profiles_reset(args)
    print(f"[forge] Unknown profiles subcommand: {args.profiles_command}", file=sys.stderr)
    return 1


def cmd_profiles_list(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    profiles_path = project_root / ".forge" / "model_profiles.yaml"
    data = load_profiles(profiles_path)
    rows = _profile_rows(data, model=args.model, role=args.role)

    if not rows:
        print("canonical_id              role       complexity  runs  successes  rate")
        return 0

    widths = {
        "canonical_id": max(len("canonical_id"), *(len(str(row["canonical_id"])) for row in rows)),
        "role": max(len("role"), *(len(str(row["role"])) for row in rows)),
        "complexity": max(len("complexity"), *(len(str(row["complexity"])) for row in rows)),
        "runs": max(len("runs"), *(len(str(row["runs"])) for row in rows)),
        "successes": max(len("successes"), *(len(str(row["successes"])) for row in rows)),
        "rate": max(len("rate"), *(len(str(row["rate"])) for row in rows)),
    }
    print(
        f"{'canonical_id':<{widths['canonical_id']}}  "
        f"{'role':<{widths['role']}}  "
        f"{'complexity':<{widths['complexity']}}  "
        f"{'runs':>{widths['runs']}}  "
        f"{'successes':>{widths['successes']}}  "
        f"{'rate':>{widths['rate']}}"
    )
    for row in rows:
        print(
            f"{row['canonical_id']:<{widths['canonical_id']}}  "
            f"{row['role']:<{widths['role']}}  "
            f"{row['complexity']:<{widths['complexity']}}  "
            f"{row['runs']:>{widths['runs']}}  "
            f"{row['successes']:>{widths['successes']}}  "
            f"{row['rate']:>{widths['rate']}}"
        )
    return 0


def cmd_profiles_reset(args: argparse.Namespace) -> int:
    canonical_id = str(args.model).strip()
    if not is_canonical_model_id(canonical_id):
        resolved = canonical_id_for_legacy_key(canonical_id)
        if resolved:
            print(
                f"[forge] {canonical_id!r} is a legacy alias. Reset by canonical ID instead: "
                f"{resolved}",
                file=sys.stderr,
            )
        else:
            print(
                f"[forge] {canonical_id!r} is not a canonical model ID. Use "
                f"<provider>/<model>/<cli|api>, for example anthropic/sonnet/cli.",
                file=sys.stderr,
            )
        return 2

    if args.role not in (None, "dev") and args.complexity is not None:
        print(
            "[forge] --complexity only applies to dev history; omit --role or use --role dev.",
            file=sys.stderr,
        )
        return 2

    project_root = Path(args.project_root).resolve()
    profiles_path = project_root / ".forge" / "model_profiles.yaml"
    reset_history_path = project_root / ".forge" / "profiles" / "reset-history.yaml"
    audit_entry = record_profile_reset(
        profiles_path=profiles_path,
        reset_history_path=reset_history_path,
        canonical_id=canonical_id,
        operator=getpass.getuser(),
        role=args.role,
        complexity=args.complexity,
        reason=args.reason,
    )

    scope = _scope_label(canonical_id, args.role, args.complexity)
    print(f"[forge] Reset {scope} history.")
    if audit_entry["pre_reset"]:
        for summary in audit_entry["pre_reset"]:
            print(f"        Pre-reset: {_format_summary(summary)}.")
    else:
        print("        Pre-reset: no matching history.")
    print(f"        Post-reset: {_post_reset_label(args.role, args.complexity)}")
    if args.reason:
        print(f"        Reason given: {args.reason}")
    print(f"        Audit record: {reset_history_path} entry at {audit_entry['timestamp']}")
    return 0


def _profile_rows(
    data: dict[str, Any],
    *,
    model: str | None = None,
    role: str | None = None,
) -> list[dict[str, Any]]:
    models = (data or {}).get("models") or {}
    if not isinstance(models, dict):
        return []
    rows: list[dict[str, Any]] = []
    model_items = sorted(models.items())
    for canonical_id, entry in model_items:
        if model is not None and canonical_id != model:
            continue
        if not isinstance(entry, dict):
            continue
        rows.extend(_entry_rows(canonical_id, entry, role=role))
    return rows


def _entry_rows(
    canonical_id: str,
    entry: dict[str, Any],
    *,
    role: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    roles = (role,) if role else ROLES
    for current_role in roles:
        section = entry.get(current_role) or {}
        if current_role == "dev":
            by = section.get("by_complexity") if isinstance(section, dict) else {}
            by = by if isinstance(by, dict) else {}
            for band in COMPLEXITY_BANDS:
                bucket = by.get(band) or {}
                if not isinstance(bucket, dict):
                    bucket = {}
                rows.append(
                    {
                        "canonical_id": canonical_id,
                        "role": "dev",
                        "complexity": band,
                        "runs": int(bucket.get("runs", 0)),
                        "successes": int(bucket.get("_successes", 0)),
                        "rate": _format_rate(
                            bucket.get("runs", 0),
                            bucket.get("success_rate", 0.0),
                        ),
                    }
                )
            continue
        if role is None and not isinstance(section, dict):
            continue
        runs = int(section.get("runs", 0)) if isinstance(section, dict) else 0
        rows.append(
            {
                "canonical_id": canonical_id,
                "role": current_role,
                "complexity": "—",
                "runs": runs,
                "successes": "—",
                "rate": "—",
            }
        )
    return rows


def _format_rate(runs: int | float, rate: float | int) -> str:
    if int(runs) <= 0:
        return "—"
    return f"{float(rate):.2f}"


def _scope_label(canonical_id: str, role: str | None, complexity: str | None) -> str:
    parts = [canonical_id]
    if role:
        parts.append(role)
    elif complexity:
        parts.append("dev")
    if complexity:
        parts.append(complexity)
    return " ".join(parts)


def _format_summary(summary: dict[str, Any]) -> str:
    role = summary["role"]
    complexity = summary.get("complexity")
    prefix = role if complexity is None else f"{role}/{complexity}"
    if role == "dev":
        return (
            f"{prefix}: {summary.get('runs', 0)} runs, {summary.get('successes', 0)} successes, "
            f"${float(summary.get('avg_cost_usd', 0.0)):.2f} avg cost, "
            f"{float(summary.get('avg_iterations', 0.0)):.1f} avg iterations"
        )
    if role == "review":
        return (
            f"{prefix}: {summary.get('runs', 0)} runs, "
            f"{float(summary.get('avg_findings', 0.0)):.1f} avg findings, "
            f"${float(summary.get('avg_cost_usd', 0.0)):.2f} avg cost"
        )
    return (
        f"{prefix}: {summary.get('runs', 0)} runs, "
        f"${float(summary.get('avg_cost_usd', 0.0)):.2f} avg cost"
    )


def _post_reset_label(role: str | None, complexity: str | None) -> str:
    if complexity is not None:
        return f"0 runs in dev/{complexity}."
    if role is not None:
        return f"0 runs in {role}."
    return "0 runs in every reset scope."
