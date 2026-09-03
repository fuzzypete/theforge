"""Command entrypoint so repository workflows enforce the same rule as the code.

``python -m theforge.spike_guard <issue-number>`` exits 0 when the issue may be
closed and 2 when it may not, printing the refusal reason on stderr. GitHub
Actions is a shell, not a Python caller, so this is how ``close-on-merge.yml``,
``close-epic-on-last-subissue.yml`` and ``enforce-spike-outcome.yml`` reach the
one implementation of the rule rather than restating it in bash.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .guard import check_spike_closure

#: Exit status for a refused close. Distinct from 1 so a usage error and a
#: refusal are not the same signal to a workflow.
REFUSED_EXIT_CODE = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m theforge.spike_guard",
        description="Check whether a GitHub issue may be closed under the spike outcome rule.",
    )
    parser.add_argument("issue", type=int, help="issue number to check")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="directory to run gh from (default: the current directory)",
    )
    args = parser.parse_args(argv)

    decision = check_spike_closure(args.issue, args.project_root)
    if decision.allowed:
        print(decision.reason)
        return 0
    print(decision.reason, file=sys.stderr)
    return REFUSED_EXIT_CODE


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
