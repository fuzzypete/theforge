"""``forge rca`` — regenerate a sprint's RCA artifact on demand.

Runs the pure RCA engine (``theforge.sprint.rca``) over a completed sprint's
local artifacts and writes ``sprint-rca.yaml`` at the sprint log root. This is
the on-demand counterpart to the eager generation that runs on sprint
completion; both call the same engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.config import load_config


def _summary_run_id(path: Path) -> str | None:
    """Return the sprint.run_id recorded inside a summary file, if readable."""
    import yaml  # noqa: PLC0415

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sprint = data.get("sprint")
    if isinstance(sprint, dict):
        rid = sprint.get("run_id")
        return str(rid) if rid else None
    return None


def cmd_rca(args: argparse.Namespace) -> int:
    """Regenerate the RCA artifact for a specific run id."""
    from theforge.sprint.rca import build_sprint_rca, write_sprint_rca
    from theforge.sprint.status_reader import find_sprint_summary

    config_path: Path | None = (
        Path(args.config).resolve() if args.config else _find_config(Path.cwd())
    )
    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1
    config = load_config(config_path)
    project_root = config.project_root

    run_id = args.run_id
    summary_path = find_sprint_summary(run_id, project_root)
    if summary_path is None:
        print(f"No sprint summary found for run id '{run_id}'.", file=sys.stderr)
        return 1

    sprint_log_dir = summary_path.parent

    # Analyse exactly the resolved summary — never the legacy pointer, which a
    # later same-name run may have overwritten. Only refresh the sprint-rca.yaml
    # pointer when the requested run *is* the latest one that pointer reflects;
    # otherwise write the durable run-keyed RCA and leave the pointer alone so a
    # historical regeneration cannot clobber the current run's analysis.
    payload = build_sprint_rca(summary_path)
    if payload is None:
        print(
            f"No non-DONE stories for run '{run_id}' — nothing to analyse.",
            file=sys.stderr,
        )
        return 0

    resolved_run_id = str(payload.get("sprint_run_id") or run_id)
    legacy_summary = sprint_log_dir / "sprint-summary.yaml"
    is_latest = legacy_summary.exists() and _summary_run_id(legacy_summary) == payload.get(
        "sprint_run_id"
    )
    target_rca = (
        sprint_log_dir / "sprint-rca.yaml"
        if is_latest
        else sprint_log_dir / f"run-{resolved_run_id}-sprint-rca.yaml"
    )

    if target_rca.exists() and not args.refresh:
        print(
            f"{target_rca.name} already exists at {target_rca}. Pass --refresh to regenerate it.",
            file=sys.stderr,
        )
        return 0

    written = write_sprint_rca(
        sprint_log_dir,
        summary_path=summary_path,
        overwrite=True,
        write_pointer=is_latest,
    )
    story_count = len(payload.get("stories") or {})
    noun = "story" if story_count == 1 else "stories"
    print(f"[forge] rca: wrote {written} ({story_count} non-DONE {noun})")
    return 0


def register_parser(subparsers: object) -> None:
    """Register the ``rca`` subcommand parser."""
    p = subparsers.add_parser(
        "rca",
        help="Regenerate a sprint's root-cause-analysis artifact (sprint-rca.yaml)",
    )
    p.add_argument("run_id", help="Sprint run id to analyse")
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument(
        "--refresh",
        action="store_true",
        default=False,
        help="Overwrite an existing sprint-rca.yaml",
    )
