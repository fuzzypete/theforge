"""REVIEW phase — GitHub PR creation helper."""

from __future__ import annotations

import logging
import subprocess

from theforge.config import ForgeConfig
from theforge.review import ReviewResult
from theforge.task import TaskStory as TaskSpec  # noqa: F401

from .state import CoordinatorState
from .util import _log

_pr_log = logging.getLogger(__name__)


def _create_pr(
    config: ForgeConfig,
    task: TaskSpec,
    branch_name: str,
    parsed_review: ReviewResult,
    state: CoordinatorState,
) -> dict:
    """Create a GitHub PR via `gh pr create`. Returns a result dict.

    Best-effort: failure returns success=False with error, never raises.
    """
    from .phase_review_finalize import _archive_story_to_done

    p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
    reviewer_names = ", ".join(p.name for p in config.review_pool)
    p2_findings = [f for f in parsed_review.findings if f.severity == "P2"]
    findings_md = ""
    if p2_findings:
        lines = []
        for f in p2_findings:
            loc = f" `{f.file}:{f.line}`" if f.file else ""
            lines.append(f"- **[P2]{loc}** {f.description}")
        findings_md = "\n".join(lines)
    else:
        findings_md = "_No findings._"

    pr_body = (
        f"## Summary\n\n"
        f"{parsed_review.summary}\n\n"
        f"## Review\n\n"
        f"- **Verdict:** APPROVE ({p1_count} P1, {p2_count} P2)\n"
        f"- **Reviewers:** {reviewer_names}\n"
        f"- **Cost:** ${state.total_cost:.2f}\n"
        f"- **Dev iterations:** {state.dev_iteration}\n"
        f"- **Tests:** N/A\n\n"
        f"## Findings\n\n"
        f"{findings_md}\n\n"
        f"## Story\n\n"
        f"{task.name} (`{task.story_path}`)\n\n"
        f"---\n"
        f"*Created automatically by [TheForge](https://github.com/fuzzypete/theforge)*"
    )

    pr_title = f"{task.name}"
    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        pr_title,
        "--body",
        pr_body,
        "--base",
        config.workspace.base_branch,
        "--head",
        branch_name,
    ]
    for label in config.workspace.pr_labels:
        cmd.extend(["--label", label])
    if config.workspace.pr_draft:
        cmd.append("--draft")

    # Archive spec from backlog/ to done/ in the feature branch so the
    # merge carries the move into main.
    worktree_dir = config.workspace.path_pattern.format(slug=task.slug)
    worktree_path = config.project_root / worktree_dir
    push_cwd = worktree_path if worktree_path.is_dir() else config.project_root
    if task.story_path:
        _archive_story_to_done(task.story_path, push_cwd, commit=True)

    # Push the feature branch to origin before creating the PR.
    try:
        push_proc = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            capture_output=True,
            text=True,
            cwd=push_cwd,
            timeout=60,
        )
        if push_proc.returncode != 0:
            err = push_proc.stderr.strip() or push_proc.stdout.strip()
            _pr_log.warning("git push failed (exit %d): %s", push_proc.returncode, err)
            return {
                "action": "pr",
                "pr_url": None,
                "success": False,
                "error": f"git push failed: {err}",
            }
    except Exception as exc:
        _pr_log.warning("git push failed: %s", exc)
        return {
            "action": "pr",
            "pr_url": None,
            "success": False,
            "error": f"git push failed: {exc}",
        }

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=push_cwd,
            timeout=60,
        )
        if proc.returncode == 0:
            pr_url = proc.stdout.strip()
            _log(f"  ✓ PR created: {pr_url}")
            return {"action": "pr", "pr_url": pr_url, "success": True, "error": None}
        else:
            err = proc.stderr.strip() or proc.stdout.strip()
            _pr_log.warning("PR creation failed (gh exited %d): %s", proc.returncode, err)
            return {"action": "pr", "pr_url": None, "success": False, "error": err}
    except Exception as exc:
        _pr_log.warning("PR creation failed: %s", exc)
        return {"action": "pr", "pr_url": None, "success": False, "error": str(exc)}
