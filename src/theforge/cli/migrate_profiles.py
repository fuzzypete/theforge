"""``forge migrate-profiles`` — canonicalize legacy model_profiles + history.

Operator-invoked, idempotent. Resolves every legacy storage key in
``.forge/model_profiles.yaml`` and ``.forge/assignment_history.yaml`` to its
canonical model ID (``<provider>/<model>/<transport.kind>``) and rewrites the
files in place. Prints a per-canonical merge report. Ambiguous keys are kept
under their legacy names with an explicit warning rather than guessed at.

This is the documented one-time release migration trigger; running it again
on already-migrated data is a no-op.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from theforge.model_profiles import (
    load_profiles,
    migrate_history_data,
    migrate_profiles_data,
    save_profiles,
)


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "migrate-profiles",
        help="Canonicalize legacy model_profiles.yaml and assignment_history.yaml keys",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root containing the .forge directory (default: cwd)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migration report without writing back to disk",
    )


def cmd_migrate_profiles(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    profiles_path = project_root / ".forge" / "model_profiles.yaml"
    history_path = project_root / ".forge" / "assignment_history.yaml"

    print(f"[migrate-profiles] project_root={project_root}")

    profiles_data = load_profiles(profiles_path)
    new_profiles, profile_report = migrate_profiles_data(profiles_data)
    _print_profile_report(profile_report)

    if not args.dry_run and profiles_path.exists():
        save_profiles(profiles_path, new_profiles)
        print(f"[migrate-profiles] wrote {profiles_path}")
    elif args.dry_run:
        print("[migrate-profiles] dry-run — profiles file not written")

    history_data: dict | None = None
    if history_path.exists():
        try:
            with open(history_path, encoding="utf-8") as f:
                history_data = yaml.safe_load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"[migrate-profiles] failed to read {history_path}: {exc}")
            history_data = None

    if isinstance(history_data, dict):
        new_history, history_report = migrate_history_data(history_data)
        _print_history_report(history_report)
        if not args.dry_run and history_report:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(history_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(new_history, f, default_flow_style=False, allow_unicode=True)
            print(f"[migrate-profiles] wrote {history_path}")
    else:
        print("[migrate-profiles] no assignment_history.yaml — skipping")

    return 0


def _reviewer_completion_note(summary: dict) -> str:
    """Render the reviewer attempt-completion counters for a bucket summary (#1388).

    Returns an empty string when the entry carries no reviewer attempt telemetry —
    the common case for already-migrated installs, where this dimension is new and
    starts empty until fresh runs record native reviewer-attempt records.
    """
    attempted = int(summary.get("review_attempted", 0) or 0)
    if attempted <= 0:
        return ""
    completed = int(summary.get("review_completed", 0) or 0)
    rate = completed / attempted if attempted else 0.0
    return f", review completion {completed}/{attempted} ({rate:.0%})"


def _print_profile_report(report: list[dict]) -> None:
    if not report:
        print("[migrate-profiles] no legacy alias entries found — already canonical")
        # Reviewer attempt-completion (#1388) is a telemetry-derived dimension:
        # it starts empty for installs without native reviewer-attempt records and
        # populates from fresh runs — there is nothing to canonicalize here.
        print(
            "[migrate-profiles] reviewer completion-rate is a new telemetry-derived "
            "dimension (_attempted_count/_completed_count/completion_rate); it starts "
            "empty for already-migrated installs and fills in from fresh runs"
        )
        return
    any_review_completion = False
    for entry in report:
        if "canonical_id" in entry:
            combined = entry["combined"]
            print(f"[migration] {entry['canonical_id']} (canonical)")
            for src in entry["merged_from"]:
                print(
                    f"  ← {src['key']} ({src['runs']} runs, "
                    f"{src['successes']:.0f} successes, "
                    f"${src['cost_usd']:.2f}{_reviewer_completion_note(src)})"
                )
            _completion = _reviewer_completion_note(combined)
            any_review_completion = any_review_completion or bool(_completion)
            print(
                f"  combined: {combined['runs']} runs, "
                f"{combined['successes']:.0f} successes, "
                f"${combined['cost_usd']:.2f}{_completion}"
            )
        elif "ambiguous_key" in entry:
            print(f"[migration] AMBIGUOUS: {entry['ambiguous_key']} — {entry['reason']}")
    if not any_review_completion:
        print(
            "[migrate-profiles] reviewer completion-rate is a new telemetry-derived "
            "dimension; no reviewer attempt records in the migrated entries yet — it "
            "fills in from fresh runs"
        )


def _print_history_report(report: list[dict]) -> None:
    if not report:
        print("[migrate-profiles] assignment_history.yaml — already canonical")
        return
    for entry in report:
        if "to" in entry:
            print(f"[history] {entry['from']!r} -> {entry['to']!r} (story={entry.get('story')})")
        elif "ambiguous_key" in entry:
            print(
                f"[history] AMBIGUOUS dev_model={entry['ambiguous_key']!r} "
                f"(story={entry.get('story')})"
            )
