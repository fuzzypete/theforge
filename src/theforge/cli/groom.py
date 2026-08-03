"""``forge groom`` — readiness repair for already-typed issues.

See ``docs/adr/0001-intake-readiness-workflow.md`` for the workflow
contract, and ``src/theforge/intake/groom_flow.py`` for the core logic.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.coordinator.audit_substrate import record_readiness_event
from theforge.intake.groom_flow import (
    GroomAction,
    GroomError,
    GroomResult,
    run_groom,
)


def _print_diff(original: str, proposed: str, label: str) -> None:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=f"{label} (before)",
        tofile=f"{label} (after)",
        n=3,
    )
    sys.stdout.writelines(diff)


def _print_summary(result: GroomResult) -> None:
    print(f"Loaded: {result.issue_ref} {result.title!r}", file=sys.stderr)
    print(f"Type: {result.issue_type or '(none)'}", file=sys.stderr)
    if result.issue_type == "bug":
        print(f"Diagnosis state: {result.bug_state.value}", file=sys.stderr)
        if result.staleness is not None:
            sha = result.staleness.baseline_sha or "(none)"
            print(
                f"Diagnosis baseline: {sha} (staleness={result.staleness.state.value})",
                file=sys.stderr,
            )
            if result.staleness.changed_files:
                changed = ", ".join(result.staleness.changed_files)
                print(f"  Changed since baseline: {changed}", file=sys.stderr)
            if result.confirm_diagnosis_current and result.staleness.is_stale:
                print(
                    "  --confirm-diagnosis-current: operator override applied "
                    "(decision recorded in audit substrate)",
                    file=sys.stderr,
                )
    print(f"Pre-groom verdict:  {result.pre_verdict.value}", file=sys.stderr)
    print(f"Post-groom verdict: {result.post_verdict.value}", file=sys.stderr)


def cmd_groom(args: argparse.Namespace) -> int:
    """Dispatch ``forge groom``. See :mod:`theforge.intake.groom_flow`."""
    issue_ref: str = args.issue
    apply_changes: bool = bool(args.apply)

    config_path = Path(args.config).resolve() if args.config else _find_config(Path.cwd())
    project_root = config_path.parent if config_path is not None else Path.cwd()

    try:
        result = run_groom(
            issue_ref,
            apply_changes=apply_changes,
            project_root=project_root,
            confirm_diagnosis_current=bool(getattr(args, "confirm_diagnosis_current", False)),
        )
    except GroomError as exc:
        print(f"[forge groom] {exc}", file=sys.stderr)
        return 1

    # Best-effort audit emission. Substrate failure should not nuke an
    # otherwise successful groom — record but continue.
    try:
        record_readiness_event(project_root, result.as_event())
    except Exception as exc:  # noqa: BLE001 — audit failure is non-fatal
        print(f"[forge groom] WARN: audit emission failed: {exc}", file=sys.stderr)

    _print_summary(result)

    if result.action is GroomAction.REFUSED:
        print(
            f"\nREFUSED: {result.refusal_reason}",
            file=sys.stdout,
        )
        if args.next:
            print(f"Next: {result.next_command}", file=sys.stdout)
        return 1

    if result.investigation_only_notice:
        print(
            "\nNOTE: This issue is investigation-ready, not implementation-ready.\n"
            "      forge groom will NOT propose the `ready` label for this state.\n"
            "      Next step: continue diagnosis until a confirmed cause is identified.",
            file=sys.stdout,
        )
        if result.staleness_notice:
            print(
                "\nNOTE: Diagnosis baseline is stale; continued investigation should\n"
                "      re-run forge diagnose to refresh ruled-out hypotheses.",
                file=sys.stdout,
            )

    if result.unsupplied_findings:
        codes = ", ".join(result.unsupplied_findings)
        print(
            f"\nUNRESOLVED: {codes}\n"
            "      groom scaffolds sections but does not invent their content, so\n"
            "      these findings still stand. Fill the placeholder sections in\n"
            "      before labeling this issue ready.",
            file=sys.stdout,
        )

    if result.needs_change:
        if apply_changes:
            print(
                "\nApplied body restructure"
                + (f" to #{result.issue_ref}" if result.issue_ref.lstrip("#").isdigit() else "")
                + ".",
                file=sys.stdout,
            )
        else:
            print("\nProposed body restructure:", file=sys.stdout)
            _print_diff(result.original_body, result.proposed_body, label=result.issue_ref)
            print(
                "\nApply this proposal? Pass --apply to commit.",
                file=sys.stdout,
            )
    else:
        print("\nNo body changes needed.", file=sys.stdout)

    if args.next:
        print(f"\nNext: {result.next_command}", file=sys.stdout)

    # Exit code policy: 0 when no change is needed OR when --apply succeeded;
    # non-zero when changes were proposed but not applied (so CI / chained
    # automation can detect "groom would do something").
    if result.needs_change and not apply_changes:
        return 2
    return 0


def register_parser(subparsers: object) -> None:
    p = subparsers.add_parser(
        "groom",
        help="Restructure a typed issue body to satisfy its shape-gate rules",
    )
    p.add_argument(
        "issue",
        help="Issue number (e.g. 1503 or #1503) or local issue body file path",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Commit the proposed body restructure via `gh issue edit`",
    )
    p.add_argument(
        "--next",
        action="store_true",
        default=False,
        help=(
            "Print a recommended next operator command "
            "(output is for humans, not a stable protocol)"
        ),
    )
    p.add_argument(
        "--confirm-diagnosis-current",
        dest="confirm_diagnosis_current",
        action="store_true",
        default=False,
        help=(
            "Operator assertion that the diagnosis is still valid against the current "
            "base, even if the recorded baseline is stale. Recorded in the audit "
            "substrate so the decision is auditable rather than silent."
        ),
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to forge.yaml (default: auto-detect)",
    )
