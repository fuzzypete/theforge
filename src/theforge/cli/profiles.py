"""``forge profiles`` — inspect and reset model capability profile history."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

from theforge.cli.shared import _find_config, load_config_checked
from theforge.config import is_canonical_model_id, load_config
from theforge.model_profiles_identity import COMPLEXITY_BANDS, ROLES
from theforge.model_profiles_storage import (
    canonical_id_for_legacy_key,
    load_profiles,
    record_profile_reset,
)
from theforge.model_strength_report import (
    DEFAULT_EVIDENCE_FLOOR,
    STATUS_UNDERPERFORMING,
    DeclaredStrengthRow,
    ModelStrengthReport,
    build_model_strength_report,
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

    strength_parser = profile_subparsers.add_parser(
        "strength",
        help="Compare declared model strength against observed dev behaviour",
    )
    strength_parser.add_argument(
        "--config",
        default=None,
        help="Path to forge.yaml (default: nearest one above cwd)",
    )
    strength_parser.add_argument(
        "--project-root",
        default=None,
        help="Project root containing the .forge directory (default: the config's)",
    )
    strength_parser.add_argument(
        "--min-runs",
        type=int,
        default=DEFAULT_EVIDENCE_FLOOR,
        help=(
            "Runs a band needs before a disagreement is claimed "
            f"(default: {DEFAULT_EVIDENCE_FLOOR})"
        ),
    )
    strength_parser.add_argument(
        "--complexity",
        choices=COMPLEXITY_BANDS,
        default=None,
        help="Show only one complexity band",
    )
    strength_parser.add_argument(
        "--model",
        default=None,
        help="Show only one canonical model ID",
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
    if args.profiles_command == "strength":
        return cmd_profiles_strength(args)
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
        print("canonical_id              role       complexity  runs  successes  rate  terminated")
        return 0

    widths = {
        "canonical_id": max(len("canonical_id"), *(len(str(row["canonical_id"])) for row in rows)),
        "role": max(len("role"), *(len(str(row["role"])) for row in rows)),
        "complexity": max(len("complexity"), *(len(str(row["complexity"])) for row in rows)),
        "runs": max(len("runs"), *(len(str(row["runs"])) for row in rows)),
        "successes": max(len("successes"), *(len(str(row["successes"])) for row in rows)),
        "rate": max(len("rate"), *(len(str(row["rate"])) for row in rows)),
        "terminated": max(len("terminated"), *(len(str(row["terminated"])) for row in rows)),
    }
    print(
        f"{'canonical_id':<{widths['canonical_id']}}  "
        f"{'role':<{widths['role']}}  "
        f"{'complexity':<{widths['complexity']}}  "
        f"{'runs':>{widths['runs']}}  "
        f"{'successes':>{widths['successes']}}  "
        f"{'rate':>{widths['rate']}}  "
        f"{'terminated':>{widths['terminated']}}"
    )
    for row in rows:
        print(
            f"{row['canonical_id']:<{widths['canonical_id']}}  "
            f"{row['role']:<{widths['role']}}  "
            f"{row['complexity']:<{widths['complexity']}}  "
            f"{row['runs']:>{widths['runs']}}  "
            f"{row['successes']:>{widths['successes']}}  "
            f"{row['rate']:>{widths['rate']}}  "
            f"{row['terminated']:>{widths['terminated']}}"
        )
    return 0


def cmd_profiles_strength(args: argparse.Namespace) -> int:
    """Render the declared-vs-observed comparison (#2308). Read-only."""
    config_path = Path(args.config).resolve() if args.config else _find_config(Path.cwd())
    if config_path is None or not config_path.exists():
        print(
            "[forge] forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1
    config = load_config_checked(
        config_path,
        loader=load_config,
        emit_startup_auth_warnings=False,
    )
    project_root = (
        Path(args.project_root).resolve() if args.project_root else Path(config.project_root)
    )
    profiles = load_profiles(project_root / ".forge" / "model_profiles.yaml")

    report = build_model_strength_report(
        model_registry=config.model_registry or {},
        profiles=profiles,
        evidence_floor=args.min_runs,
        recency=getattr(config.assignment, "recency", None),
    )
    print(_render_strength(report, model=args.model, complexity=args.complexity))
    return 0


def _render_strength(
    report: ModelStrengthReport,
    *,
    model: str | None = None,
    complexity: str | None = None,
) -> str:
    rows = [
        row
        for row in report.rows
        if (model is None or row.canonical_id == model)
        and (complexity is None or row.complexity == complexity)
    ]
    lines: list[str] = []
    headers = ("model", "band", "declared", "observed", "samples", "declared peers", "status")
    table = [
        (
            row.canonical_id,
            row.complexity,
            f"{row.declared_tier}/{row.declared_capability}",
            _rate(row.observed_rate),
            str(row.runs),
            _peer_range(row),
            row.status,
        )
        for row in rows
    ]
    lines.extend(_table_lines(headers, table))

    lines.append("")
    lines.append(
        f"Evidence floor: {report.evidence_floor} runs per band — below it a band is reported "
        "as observed-but-insufficient, never as disagreement."
    )
    lines.append(
        "Evidence recency: unknown — profiles record no per-key timestamp, so a rate may rest "
        "on history that stopped accruing."
    )

    legacy = [row for row in rows if _legacy_keys(row)]
    if legacy:
        lines.append("")
        lines.append("Evidence drawn partly from non-canonical keys:")
        for row in legacy:
            keys = ", ".join(_legacy_keys(row))
            lines.append(f"  {row.canonical_id} {row.complexity}: {keys}")

    disagreements = [row for row in rows if row.status == STATUS_UNDERPERFORMING]
    lines.append("")
    if disagreements:
        lines.append(f"Declarations the evidence disagrees with: {len(disagreements)}")
        for row in disagreements:
            lines.append(
                f"  {row.canonical_id} {row.complexity}: declared "
                f"{row.declared_tier}/{row.declared_capability}, observed "
                f"{_rate(row.observed_rate)} over {row.runs} runs; declared peers "
                f"{_peer_range(row)}"
            )
    else:
        lines.append("Declarations the evidence disagrees with: none")

    if report.unattributed:
        lines.append("")
        lines.append("Profile evidence excluded — not attributable to a live dev-capable model:")
        for entry in report.unattributed:
            resolved = entry.canonical_id or "unresolved"
            lines.append(
                f"  {entry.key} ({entry.reason}, resolves to {resolved}): {entry.runs} runs"
            )

    if report.excluded_non_dev_models:
        lines.append("")
        lines.append(
            f"Live models excluded as not dev-capable ({len(report.excluded_non_dev_models)}): "
            + ", ".join(report.excluded_non_dev_models)
        )

    lines.append("")
    lines.append("Advisory only — this command does not modify any catalog declaration.")
    return "\n".join(lines)


def _table_lines(headers: tuple[str, ...], table: list[tuple[str, ...]]) -> list[str]:
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in table)) if table else len(headers[i])
        for i in range(len(headers))
    ]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in table:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _peer_range(row: DeclaredStrengthRow) -> str:
    if row.peer_low is None or row.peer_high is None:
        return "—"
    return f"{row.peer_low:.2f}–{row.peer_high:.2f} ({row.peer_count})"


def _legacy_keys(row: DeclaredStrengthRow) -> list[str]:
    return [key for key in row.contributing_keys if key != row.canonical_id]


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
                        # Harness-imposed terminations (deadline kill / stuck-
                        # pattern terminate / coordinator iteration budget spent
                        # without submit) are recorded but excluded from runs/
                        # rate (#1763, #2921), so surface the tally separately
                        # here. The per-cause breakdown lives under
                        # ``harness_terminated.by_cause`` in model_profiles.yaml.
                        "terminated": int((bucket.get("harness_terminated") or {}).get("runs", 0)),
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
                "terminated": "—",
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


def _unmeasured_suffix(summary: dict[str, Any]) -> str:
    """`` (N cost-unmeasured)`` note so operators can tell free from unmeasured."""
    unknown = int(summary.get("cost_unknown_runs", 0))
    return f" ({unknown} cost-unmeasured)" if unknown > 0 else ""


def _format_summary(summary: dict[str, Any]) -> str:
    role = summary["role"]
    complexity = summary.get("complexity")
    prefix = role if complexity is None else f"{role}/{complexity}"
    suffix = _unmeasured_suffix(summary)
    if role == "dev":
        return (
            f"{prefix}: {summary.get('runs', 0)} runs, {summary.get('successes', 0)} successes, "
            f"${float(summary.get('avg_cost_usd', 0.0)):.2f} avg cost{suffix}, "
            f"{float(summary.get('avg_iterations', 0.0)):.1f} avg iterations"
        )
    if role == "review":
        return (
            f"{prefix}: {summary.get('runs', 0)} runs, "
            f"{float(summary.get('avg_findings', 0.0)):.1f} avg findings, "
            f"${float(summary.get('avg_cost_usd', 0.0)):.2f} avg cost{suffix}"
        )
    return (
        f"{prefix}: {summary.get('runs', 0)} runs, "
        f"${float(summary.get('avg_cost_usd', 0.0)):.2f} avg cost{suffix}"
    )


def _post_reset_label(role: str | None, complexity: str | None) -> str:
    if complexity is not None:
        return f"0 runs in dev/{complexity}."
    if role is not None:
        return f"0 runs in {role}."
    return "0 runs in every reset scope."
