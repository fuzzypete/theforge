"""Subprocess entry point for project-code evaluations that run in the worktree.

Called via:
    PYTHONPATH=/worktree/src python /path/to/_subprocess_eval.py <command>

with JSON piped to stdin; JSON result printed to stdout.

By setting PYTHONPATH to the worktree's src/ directory, the subprocess imports
theforge.* modules from the worktree rather than the coordinator's own copies.
This is the correct isolation boundary for self-hosting: the coordinator never
imports worktree-version code into its own process; it only evaluates it here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# When Python runs this file directly (not via -m), it inserts the script's
# directory into sys.path[0].  The coordinator directory contains logging.py
# which shadows the standard library logging module.  Remove it before any
# lazy imports inside the command handlers below.
_this_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == _this_dir:
    sys.path.pop(0)


def _cmd_check_conventions(data: dict) -> dict:
    """Run check_hard_conventions or new_hard_convention_violations_since_ref."""
    from theforge.config.types import HardConventionsConfig
    from theforge.conventions import (
        check_hard_conventions,
        new_hard_convention_violations_since_ref,
    )
    from theforge.line_count_conventions import fail_closed_module_violations

    config = HardConventionsConfig(**data["config"])
    project_root = Path(data["project_root"])

    def _v_to_dict(v: object) -> dict:
        return {
            "rule": v.rule,  # type: ignore[attr-defined]
            "file": v.file,  # type: ignore[attr-defined]
            "detail": v.detail,  # type: ignore[attr-defined]
            "blocking": v.blocking,  # type: ignore[attr-defined]
        }

    if "baseline_ref" in data:
        all_v, net_new_v = new_hard_convention_violations_since_ref(
            config, project_root, data["baseline_ref"]
        )
        return {
            "all_violations": [_v_to_dict(v) for v in all_v],
            "violations": [_v_to_dict(v) for v in net_new_v],
        }
    else:
        # No baseline tree, so no frozen ceiling can be derived. The advisory
        # view keeps the plain scan (distance from the configured limit); the
        # blocking view fails closed at that limit rather than letting an
        # oversized module pass unchecked (ADR-0008).
        violations = check_hard_conventions(config, project_root)
        return {
            "all_violations": [_v_to_dict(v) for v in violations],
            "violations": [_v_to_dict(v) for v in fail_closed_module_violations(violations)],
        }


_COMMANDS = {
    "check_conventions": _cmd_check_conventions,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in _COMMANDS:
        print(json.dumps({"error": f"unknown command: {cmd!r}"}), file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"JSON decode error: {e}"}), file=sys.stderr)
        sys.exit(1)

    try:
        result = _COMMANDS[cmd](payload)
        print(json.dumps(result))
    except Exception as e:
        import traceback

        print(
            json.dumps({"error": str(e), "traceback": traceback.format_exc()}),
            file=sys.stderr,
        )
        sys.exit(1)
