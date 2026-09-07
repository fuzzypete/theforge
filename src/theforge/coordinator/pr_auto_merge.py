"""Arming a pull request for auto-merge (#2818).

Forge does not merge into a protected base branch on its own authority. It opens
a pull request and *arms* it — ``gh pr merge --auto`` — which hands the decision
to the branch's own requirements: the carrier lands when they are satisfied, and
a branch that refuses stays refused. That is one mechanism with one shape, and it
is used by every carrier forge opens into the base branch, whether the carrier
holds a story's reviewed source changes or the run's project memory.

It lives here rather than in ``completion.py`` so the memory-publication path can
reach it without depending on story completion. ``completion.py`` re-exports
these names because that is where its callers and tests already reach for them.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .exception_context import _build_exception_context

_pr_log = logging.getLogger(__name__)


_AUTO_MERGE_ARMING_MARKERS = (
    "enablepullrequestautomerge",
    "protected branch rules not configured",
    "auto-merge is not allowed",
    "auto merge is not allowed",
    "allow_auto_merge",
    "allowautomerge",
)

_AUTO_MERGE_ARMING_HINT = (
    "Auto-merge arming was refused by GitHub; the PR itself was not rejected. "
    "Configure branch protection on the target branch (enable required status "
    "checks / allow auto-merge) or merge the PR manually with "
    "`gh pr merge <PR> --squash --delete-branch` (without `--auto`)."
)


def _is_auto_merge_arming_error(stderr_text: str) -> bool:
    """Return True if ``gh pr merge --auto`` stderr indicates the *arming*
    step failed (rather than the PR being rejected on its merits)."""
    if not stderr_text:
        return False
    lowered = stderr_text.lower()
    return any(marker in lowered for marker in _AUTO_MERGE_ARMING_MARKERS)


def _step_merge(project_root: Path, pr_url: str, merge_strategy: str) -> dict:
    """Execute ``gh pr merge --auto --{strategy}``.

    Returns ``{"success": True}`` or
    ``{"success": False, "error": ..., "retryable": bool, "arming_failed": bool}``.

    ``arming_failed`` is True when GitHub refused to *arm* auto-merge (e.g.
    target branch lacks the protection rules ``enablePullRequestAutoMerge``
    requires) — distinct from a genuine PR rejection.
    """
    merge_retry_error = "base branch was modified"
    try:
        merge_proc = subprocess.run(
            ["gh", "pr", "merge", pr_url, "--auto", f"--{merge_strategy}"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=120,
        )
    except Exception as exc:
        _pr_log.warning("gh pr merge --auto failed: %s", exc)
        return {
            "success": False,
            "error": f"gh pr merge failed: {exc}",
            "retryable": False,
            "arming_failed": False,
            "error_context": _build_exception_context(
                exc,
                cmd=["gh", "pr", "merge", pr_url, "--auto", f"--{merge_strategy}"],
            ),
        }

    if merge_proc.returncode != 0:
        err = "\n".join(
            part.strip()
            for part in (merge_proc.stderr, merge_proc.stdout)
            if part and part.strip()
        )
        _pr_log.warning("gh pr merge --auto failed (exit %d): %s", merge_proc.returncode, err)
        arming_failed = _is_auto_merge_arming_error(err)
        if arming_failed:
            error_msg = f"{_AUTO_MERGE_ARMING_HINT} Underlying error: {err}"
        else:
            error_msg = f"gh pr merge failed: {err}"
        return {
            "success": False,
            "error": error_msg,
            "retryable": merge_retry_error in err.lower(),
            "arming_failed": arming_failed,
        }

    return {"success": True}
