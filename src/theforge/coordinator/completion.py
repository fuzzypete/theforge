"""Story completion helpers: archive, PR creation, cycle history, and finalize-approve."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.review import ReviewResult
from theforge.task import TaskStory

from .github_integration import assign_pr_reviewers, post_findings_comment
from .logging import StructuredLogger
from .notify import _ntfy_done_notify
from .state import CoordinatorResult, CoordinatorState, CycleHistory, Phase
from .util import _fmt_duration, _log, _log_verbose
from .workspace import _deindex_forge_artifacts, _merge_branch

_pr_log = logging.getLogger(__name__)
MAX_MERGE_RETRIES = 3


def _archive_story_to_done(
    story_path: "str | Path",
    cwd: Path,
    *,
    commit: bool = False,
) -> bool:
    """Move a story file from backlog/ to done/ via git mv.

    Returns True if the move succeeded, False otherwise (best-effort).
    When *commit* is True a small git commit is created for the move.
    """
    if story_path is None:
        return False  # nothing to archive for issue-sourced stories
    src = Path(story_path)
    # Only move files that live under specs/backlog/
    try:
        rel = src.relative_to(cwd)
    except ValueError:
        # Absolute path — try making it relative
        rel = src
    parts = rel.parts
    if "backlog" not in parts:
        return False
    # Build destination: replace 'backlog' with 'done'
    idx = parts.index("backlog")
    dest_parts = parts[:idx] + ("done",) + parts[idx + 1 :]
    dest = Path(*dest_parts)
    dest_abs = cwd / dest
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "mv", str(rel), str(dest)],
            cwd=str(cwd),
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0:
            _log_verbose(f"  story archive git mv failed: {proc.stderr.decode().strip()}")
            return False
        _log(f"  Archived story: {rel} → {dest}")
        if commit:
            subprocess.run(
                ["git", "commit", "-m", f"chore: archive {rel.name} to done/"],
                cwd=str(cwd),
                capture_output=True,
                timeout=15,
            )
        return True
    except Exception as exc:
        _log_verbose(f"  story archive failed: {exc}")
        return False


def _branch_has_unique_commits(
    push_cwd: Path, base_branch: str, branch_name: str
) -> tuple[bool, str | None]:
    """Return whether branch has commits not reachable from base_branch.

    Returns (has_unique_commits, error). On git failure, error is populated and
    callers should treat the check as failed rather than assuming zero delta.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{base_branch}..{branch_name}"],
            capture_output=True,
            text=True,
            cwd=str(push_cwd),
            timeout=30,
        )
    except Exception as exc:
        return False, f"git rev-list failed: {exc}"

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        return False, f"git rev-list failed: {err}"

    output = proc.stdout.strip()
    try:
        return int(output) > 0, None
    except ValueError:
        return False, f"git rev-list returned non-integer count: {output!r}"


def _create_pr(
    config: ForgeConfig,
    task: TaskStory,
    branch_name: str,
    parsed_review: ReviewResult,
    state: CoordinatorState,
) -> dict:
    """Create a GitHub PR via `gh pr create`. Returns a result dict.

    Best-effort: failure returns success=False with error, never raises.
    """
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

    closes_line = f"\n\nCloses #{task.github_issue}" if task.github_issue else ""
    if task.story_path is None and task.github_issue:
        story_line = f"{task.name} (GitHub Issue #{task.github_issue})"
    else:
        story_line = f"{task.name} (`{task.story_path}`)"
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
        f"{story_line}\n\n"
        f"---\n"
        f"*Created automatically by [TheForge](https://github.com/fuzzypete/theforge)*"
        f"{closes_line}"
    )

    pr_title = f"{task.name}"

    try:
        merged_pr_proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                "closed",
                "--json",
                "number,url,mergedAt",
            ],
            capture_output=True,
            text=True,
            cwd=config.project_root,
            timeout=30,
        )
        if merged_pr_proc.returncode == 0:
            merged_prs = json.loads(merged_pr_proc.stdout or "[]")
            merged_pr = next((pr for pr in merged_prs if pr.get("mergedAt")), None)
            if merged_pr is not None:
                pr_url = merged_pr.get("url")
                message = f"PR already merged for branch {branch_name}: {pr_url}"
                _pr_log.warning(message)
                return {"action": "pr", "pr_url": pr_url, "success": False, "error": message}
        else:
            err = merged_pr_proc.stderr.strip() or merged_pr_proc.stdout.strip()
            _pr_log.warning(
                "Merged PR lookup failed for %s (gh exited %d): %s",
                branch_name,
                merged_pr_proc.returncode,
                err,
            )
    except Exception as exc:
        _pr_log.warning("Merged PR lookup failed for %s: %s", branch_name, exc)

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

    has_unique_commits, commit_check_error = _branch_has_unique_commits(
        push_cwd, config.workspace.base_branch, branch_name
    )
    if commit_check_error is not None:
        _pr_log.warning("branch delta check failed: %s", commit_check_error)
        return {
            "action": "pr",
            "pr_url": None,
            "success": False,
            "error": commit_check_error,
        }
    if not has_unique_commits:
        _log(
            f"  Skipping PR creation: branch {branch_name} has no commits ahead of "
            f"origin/{config.workspace.base_branch}"
        )
        return {
            "action": "pr",
            "pr_url": None,
            "success": True,
            "error": None,
            "skipped": True,
            "skip_reason": "zero-delta branch",
            "ahead_commit_count": 0,
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
            if config.github.enabled:
                reviewer_result = assign_pr_reviewers(pr_url, config.review_pool, push_cwd)
                if not reviewer_result["success"]:
                    _pr_log.warning(
                        "GitHub reviewer assignment failed (non-fatal): %s",
                        reviewer_result["error"],
                    )
                comment_result = post_findings_comment(
                    pr_url, parsed_review, config.review_pool, push_cwd
                )
                if not comment_result["success"]:
                    _pr_log.warning(
                        "GitHub findings comment failed (non-fatal): %s", comment_result["error"]
                    )
            return {"action": "pr", "pr_url": pr_url, "success": True, "error": None}
        else:
            err = proc.stderr.strip() or proc.stdout.strip()
            _pr_log.warning("PR creation failed (gh exited %d): %s", proc.returncode, err)
            return {"action": "pr", "pr_url": None, "success": False, "error": err}
    except Exception as exc:
        _pr_log.warning("PR creation failed: %s", exc)
        return {"action": "pr", "pr_url": None, "success": False, "error": str(exc)}


def _merge_pr(
    config: ForgeConfig,
    task: TaskStory,
    branch_name: str,
    parsed_review: ReviewResult,
    state: CoordinatorState,
) -> dict:
    """Create a PR and immediately merge it via gh pr merge.

    Sequence:
    1. Fetch + rebase onto latest origin/{base_branch} (escalate on conflict).
    2. Force-push rebased branch so _create_pr's push is a fast-forward.
    3. Call _create_pr() to archive story, push, and open the PR.
    4. Merge via `gh pr merge --{strategy}` from the project root.
    5. Best-effort local cleanup: fast-forward local base_branch, remove the
       feature worktree, and delete the feature branch locally. Remote branch
       deletion is deferred unless the PR is already merged.

    Returns a result dict with keys: action, pr_url, merged, success, error.
    Never raises.
    """
    base_branch = config.workspace.base_branch
    merge_strategy = config.workspace.merge_strategy
    worktree_dir = config.workspace.path_pattern.format(slug=task.slug)
    worktree_path = config.project_root / worktree_dir
    push_cwd = worktree_path if worktree_path.is_dir() else config.project_root

    def _fail(error: str, *, pr_url: str | None = None, merged: bool = False) -> dict:
        return {
            "action": "merge-pr",
            "pr_url": pr_url,
            "merged": merged,
            "success": False,
            "error": error,
        }

    def _cleanup_after_merge(*, delete_remote_branch: bool) -> None:
        """Best-effort local cleanup after a successful remote PR merge."""
        try:
            subprocess.run(
                ["git", "fetch", "origin"],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=60,
            )
            subprocess.run(
                ["git", "merge", "--ff-only", f"origin/{base_branch}"],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=30,
            )
        except Exception as exc:
            _pr_log.warning("local base_branch fast-forward failed (non-fatal): %s", exc)

        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=30,
            )
        except Exception as exc:
            _pr_log.warning("worktree cleanup failed (non-fatal): %s", exc)

        try:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=30,
            )
        except Exception as exc:
            _pr_log.warning("local branch cleanup failed (non-fatal): %s", exc)

        if not delete_remote_branch:
            return

        try:
            subprocess.run(
                ["git", "push", "origin", "--delete", branch_name],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=60,
            )
        except Exception as exc:
            _pr_log.warning("remote branch cleanup failed (non-fatal): %s", exc)

    pr_url: str | None = None
    auto_merge_queued = False
    merge_retry_error = "base branch was modified"

    for attempt in range(MAX_MERGE_RETRIES):
        # Step 1: defensively scrub tracked forge artifacts before rebase.
        _deindex_forge_artifacts(push_cwd)

        # Step 2: fetch + rebase onto latest base_branch
        try:
            fetch_proc = subprocess.run(
                ["git", "fetch", "origin", base_branch],
                capture_output=True,
                text=True,
                cwd=str(push_cwd),
                timeout=60,
            )
            if fetch_proc.returncode != 0:
                err = fetch_proc.stderr.strip() or fetch_proc.stdout.strip()
                _pr_log.warning("git fetch failed (exit %d): %s", fetch_proc.returncode, err)
                return _fail(f"git fetch failed: {err}", pr_url=pr_url)

            rebase_proc = subprocess.run(
                ["git", "rebase", f"origin/{base_branch}"],
                capture_output=True,
                text=True,
                cwd=str(push_cwd),
                timeout=120,
            )
            if rebase_proc.returncode != 0:
                err = rebase_proc.stderr.strip() or rebase_proc.stdout.strip()
                _pr_log.warning("git rebase failed (exit %d): %s", rebase_proc.returncode, err)
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    capture_output=True,
                    cwd=str(push_cwd),
                    timeout=30,
                )
                return _fail(
                    f"rebase onto {base_branch} failed — escalating: {err}",
                    pr_url=pr_url,
                )
        except Exception as exc:
            _pr_log.warning("rebase step failed: %s", exc)
            return _fail(f"rebase step failed: {exc}", pr_url=pr_url)

        # Step 3: force-push the rebased branch so _create_pr's push is a fast-forward
        try:
            push_proc = subprocess.run(
                ["git", "push", "-f", "origin", branch_name],
                capture_output=True,
                text=True,
                cwd=str(push_cwd),
                timeout=60,
            )
            if push_proc.returncode != 0:
                err = push_proc.stderr.strip() or push_proc.stdout.strip()
                _pr_log.warning("force-push failed (exit %d): %s", push_proc.returncode, err)
                return _fail(f"force-push after rebase failed: {err}", pr_url=pr_url)
        except Exception as exc:
            _pr_log.warning("force-push failed: %s", exc)
            return _fail(f"force-push after rebase failed: {exc}", pr_url=pr_url)

        # Step 3: create the PR only once (also archives story + pushes archive commit)
        if attempt == 0:
            pr_result = _create_pr(config, task, branch_name, parsed_review, state)
            if not pr_result.get("success"):
                return _fail(
                    pr_result.get("error") or "PR creation failed",
                    pr_url=pr_result.get("pr_url"),
                )
            pr_url = pr_result["pr_url"]

        # Step 4: defensively scrub tracked forge artifacts again after rebase,
        # before any push/merge consumes the rewritten commit graph.
        _deindex_forge_artifacts(push_cwd)

        # Step 5: merge the PR remotely from the repo root. Running gh from the
        # feature worktree can trip worktree branch checkout constraints.
        try:
            merge_proc = subprocess.run(
                ["gh", "pr", "merge", pr_url, "--auto", f"--{merge_strategy}"],
                capture_output=True,
                text=True,
                cwd=str(config.project_root),
                timeout=120,
            )
        except Exception as exc:
            _pr_log.warning("gh pr merge failed: %s", exc)
            return _fail(f"gh pr merge failed: {exc}", pr_url=pr_url)

        if merge_proc.returncode == 0:
            merge_output = "\n".join(
                part.strip()
                for part in (merge_proc.stdout, merge_proc.stderr)
                if part and part.strip()
            ).lower()
            auto_merge_queued = (
                "auto-merge enabled" in merge_output
                or "pull request is not mergeable" in merge_output
            )
            break

        err = "\n".join(
            part.strip()
            for part in (merge_proc.stderr, merge_proc.stdout)
            if part and part.strip()
        )
        _pr_log.warning("gh pr merge failed (exit %d): %s", merge_proc.returncode, err)
        if merge_retry_error in err.lower():
            if attempt < MAX_MERGE_RETRIES - 1:
                _pr_log.warning(
                    "base branch changed during PR merge attempt %d/%d; retrying",
                    attempt + 1,
                    MAX_MERGE_RETRIES,
                )
                continue
            return _fail(f"gh pr merge failed: {err}", pr_url=pr_url)
        return _fail(f"gh pr merge failed: {err}", pr_url=pr_url)

    if auto_merge_queued:
        _log(f"  ✓ PR queued for auto-merge: {pr_url}")
    else:
        _log(f"  ✓ PR merged: {pr_url}")

    # Step 6: sync local state and clean up the merged feature worktree/branch.
    # Preserve the remote branch when GitHub only queued auto-merge; branch
    # protection still needs that ref until the hosted merge completes.
    _cleanup_after_merge(delete_remote_branch=not auto_merge_queued)

    return {
        "action": "merge-pr",
        "pr_url": pr_url,
        "merged": True,
        "success": True,
        "error": None,
        "auto_merge_queued": auto_merge_queued,
    }


def _append_cycle_history(state: CoordinatorState, parsed_review: ReviewResult) -> None:
    """Append a CycleHistory entry for this completed review cycle (capped at 3)."""
    state.cycle_history_total += 1
    entry = CycleHistory(
        cycle=state.cycle_history_total,
        verdict=parsed_review.verdict,
        summary=parsed_review.summary,
        p1_findings=[f.description[:200] for f in parsed_review.findings if f.severity == "P1"],
    )
    state.cycle_history.append(entry)
    if len(state.cycle_history) > 3:
        state.cycle_history = state.cycle_history[-3:]


def _finalize_approve(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    parsed_review: ReviewResult,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: "StructuredLogger | None",
    review_cost: float,
    review_elapsed: float,
    message: str,
    run_id: str = "",
) -> CoordinatorResult:
    """Set DONE, optionally merge, log, notify, return CoordinatorResult.

    Pass logger=None to suppress merge_result/phase_end logger events (interactive paths).
    Pass logger=logger to emit them (non-interactive path).
    """
    state.phase = Phase.DONE
    merge_info: dict | None = None
    merge_suffix = ""

    # Resolve effective on_approve: CLI --auto-merge flag forces "merge"
    effective_on_approve = "merge" if auto_merge else config.workspace.on_approve

    if effective_on_approve == "merge":
        merge_info = _merge_branch(
            config.project_root,
            config.workspace.base_branch,
            branch_name,
            task.slug,
            workspace_path,
            auto_push=config.workspace.auto_push,
            config=config,
            task_name=task.name,
        )
        merge_info = dict(merge_info)
        merge_info["action"] = "merge"
        merge_suffix = (
            " Merged." if merge_info["merged"] else f" Merge failed: {merge_info['error']}"
        )
        if merge_info["merged"] and task.story_path:
            _archive_story_to_done(task.story_path, config.project_root, commit=True)
        if logger:
            logger._safe_emit(
                "merge_result",
                success=merge_info["merged"],
                branch=branch_name,
                error=merge_info.get("error"),
            )
        if merge_info["merged"] and config.hooks and config.hooks.post_merge:
            from .hooks import build_post_merge_payload
            from .hooks import run_hook as _run_hook

            _pm_payload = build_post_merge_payload(task.slug, branch_name, run_id, config)
            _run_hook(
                config.hooks.post_merge,
                _pm_payload,
                config.hooks.timeout_seconds,
                "post_merge",
                logger,
                secrets=config.secrets,
            )
    elif effective_on_approve == "merge-pr":
        merge_info = _merge_pr(config, task, branch_name, parsed_review, state)
        if merge_info["merged"]:
            merge_suffix = f" PR merged: {merge_info['pr_url']}"
        else:
            merge_suffix = f" merge-pr failed: {merge_info['error']}"
        if logger:
            logger._safe_emit(
                "merge_result",
                success=merge_info["merged"],
                branch=branch_name,
                pr_url=merge_info.get("pr_url"),
                error=merge_info.get("error"),
            )
        if merge_info["merged"] and config.hooks and config.hooks.post_merge:
            from .hooks import build_post_merge_payload
            from .hooks import run_hook as _run_hook

            _pm_payload = build_post_merge_payload(task.slug, branch_name, run_id, config)
            _run_hook(
                config.hooks.post_merge,
                _pm_payload,
                config.hooks.timeout_seconds,
                "post_merge",
                logger,
                secrets=config.secrets,
            )
        if not merge_info["merged"]:
            # PR merge failed (rebase conflict, gh error, etc.) — escalate rather than DONE.
            state.phase = Phase.ESCALATE
            state.error = merge_info.get("error") or "merge-pr failed"
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="REVIEW",
                    outcome="escalate",
                    cost_usd=round(review_cost, 6),
                    duration_s=round(review_elapsed, 2),
                )
            return CoordinatorResult(
                success=False,
                phase=Phase.ESCALATE,
                state=state,
                message=f"{message}Branch: {branch_name}{merge_suffix}",
                merge=merge_info,
            )
    elif effective_on_approve == "pr":
        merge_info = _create_pr(config, task, branch_name, parsed_review, state)
        if merge_info.get("skipped"):
            merge_suffix = " PR skipped: zero-delta branch"
        elif merge_info["success"]:
            merge_suffix = f" PR: {merge_info['pr_url']}"
        else:
            merge_suffix = f" PR creation failed: {merge_info['error']}"
    else:
        # "none" — leave branch, log name
        _log(f"  Branch ready for manual review: {branch_name}")
        merge_info = {"action": "none", "success": True, "error": None}
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="approve",
            cost_usd=round(review_cost, 6),
            duration_s=round(review_elapsed, 2),
        )
    _task_elapsed = time.monotonic() - task_start
    _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_task_elapsed)}")
    _ntfy_done_notify(
        task, state, config, notify, parsed_review.summary, _task_elapsed, branch_name
    )
    return CoordinatorResult(
        success=True,
        phase=state.phase,
        state=state,
        message=f"{message}Branch: {branch_name}{merge_suffix}",
        merge=merge_info,
    )
