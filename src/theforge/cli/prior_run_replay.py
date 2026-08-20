"""``forge prior-run-replay`` - historical measurement for prior-run selection."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.prior_run_replay import CorpusSpec, PriorRunReplayError, run_prior_run_replay
from theforge.prior_run_replay_render import render_json, render_terminal, render_yaml


def cmd_prior_run_replay(args: argparse.Namespace) -> int:
    config_path = _find_config(Path(args.config).resolve() if args.config else None)
    if config_path is None:
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    if not args.corpus:
        print("At least one --corpus NAME=PATH is required", file=sys.stderr)
        return 1

    judgments_path = Path(args.judgments).resolve()
    if not judgments_path.is_file():
        print(f"Judgments file not found: {judgments_path}", file=sys.stderr)
        return 1

    try:
        corpora = [_parse_corpus(value) for value in args.corpus]
        report = run_prior_run_replay(corpora, judgments_path=judgments_path)
    except PriorRunReplayError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.format == "terminal":
        print(render_terminal(report), end="")
    elif args.format == "json":
        print(render_json(report), end="")
    else:
        print(render_yaml(report), end="")
    return 0


def _parse_corpus(value: str) -> CorpusSpec:
    if "=" not in value:
        raise PriorRunReplayError(f"invalid --corpus {value!r}; expected NAME=PATH")
    name, raw_path = value.split("=", 1)
    name = name.strip()
    path = Path(raw_path).resolve()
    if not name:
        raise PriorRunReplayError(f"invalid --corpus {value!r}; missing corpus name")
    if not path.is_dir():
        raise PriorRunReplayError(f"corpus root not found: {path}")
    return CorpusSpec(name=name, root=path)


def register_parser(subparsers: object) -> None:
    p = subparsers.add_parser(
        "prior-run-replay",
        help="Replay historical stories through select_prior_runs for evidence-based tuning",
    )
    p.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
    p.add_argument(
        "--corpus",
        action="append",
        metavar="NAME=PATH",
        help="Named corpus root to replay; repeat for multiple corpora",
    )
    p.add_argument(
        "--judgments",
        required=True,
        help="Path to the replay judgment YAML",
    )
    p.add_argument(
        "--format",
        choices=("terminal", "yaml", "json"),
        default="terminal",
        help="Output format (default: terminal)",
    )
