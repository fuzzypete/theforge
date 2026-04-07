"""forge index subcommand — generate a structural module index."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theforge.cli.shared import _find_config
from theforge.indexer import INDEX_PATH, generate_index


def cmd_index(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve() if getattr(args, "config", None) else _find_config()
    if config_path is None or not config_path.exists():
        print(
            "forge.yaml not found. Run 'forge init' to create one, "
            "or pass --config path/to/forge.yaml",
            file=sys.stderr,
        )
        return 1

    project_root = config_path.parent
    generate_index(project_root)
    print(str(project_root / INDEX_PATH))
    return 0


def register_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("index", help="Generate .forge/index/modules.yaml")
    parser.add_argument("--config", help="Path to forge.yaml (default: auto-detect)")
