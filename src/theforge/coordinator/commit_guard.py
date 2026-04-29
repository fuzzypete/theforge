"""Zero-commit guards: detect empty branches before they reach DONE.

A dev iteration that produces zero commits ahead of the base branch must not
flow through to APPROVE/DONE. The guards in this module are invoked at the
DEV → VALIDATE, VALIDATE → REVIEW, and REVIEW → DONE seams to escalate empty
runs early instead of letting integration silently skip PR creation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _has_commits_ahead_of_base(workspace_path: Path, base_branch: str) -> bool:
    """Return True if HEAD has commits not reachable from base_branch.

    Tries `origin/{base_branch}` first, then falls back to the local ref. If
    both git invocations fail, returns True (fail-open) so transient git
    failures do not trigger spurious escalations on otherwise healthy runs.
    """
    for ref in (f"origin/{base_branch}", base_branch):
        try:
            proc = subprocess.run(
                ["git", "rev-list", "--count", f"{ref}..HEAD"],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            continue
        if proc.returncode != 0:
            continue
        try:
            return int(proc.stdout.strip() or "0") > 0
        except ValueError:
            continue
    return True
