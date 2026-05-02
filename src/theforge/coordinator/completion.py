"""Story completion helpers: archive, PR creation, cycle history, and finalize-approve."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
import traceback
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.review import ReviewResult
from theforge.task import TaskStory

from .gate import _parse_dirty_files
from .github_integration import assign_pr_reviewers, post_findings_comment
from .logging import StructuredLogger
from .notify import _ntfy_done_notify
from .run_setup import delete_merge_state, load_merge_state, save_merge_state
from .state import CoordinatorResult, CoordinatorState, CycleHistory, MergeStepState, Phase
from .util import _fmt_duration, _log, _log_verbose, _run_shell
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


def _render_command_for_audit(cmd: list[str]) -> str:
    """Render a subprocess command line while redacting bulky free-text args."""
    rendered: list[str] = []
    skip_next = False
    for index, arg in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if arg == "--body" and index + 1 < len(cmd):
            rendered.append(shlex.quote(arg))
            rendered.append(shlex.quote(f"<redacted body len={len(cmd[index + 1])}>"))
            skip_next = True
            continue
        rendered.append(shlex.quote(arg))
    return " ".join(rendered)


def _escape_control_preview(text: str) -> str:
    """Render a preview string with control characters made explicit."""
    out: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            out.append(f"\\x{codepoint:02x}")
        else:
            out.append(char)
    return "".join(out)


def _preview_window(text: str, position: int, *, radius: int = 24) -> str:
    """Return an escaped preview around a string position."""
    start = max(0, position - radius)
    end = min(len(text), position + radius + 1)
    return _escape_control_preview(text[start:end])


def _hex_window(text: str, position: int, *, radius: int = 8) -> str:
    """Return a compact hex dump window around a string position."""
    start = max(0, position - radius)
    end = min(len(text), position + radius + 1)
    return text[start:end].encode("utf-8", errors="replace").hex(" ")


def _identify_embedded_null_source(
    cmd: list[str],
    input_sources: list[dict[str, str]] | None,
) -> dict[str, object] | None:
    """Locate the first embedded NUL in the command and source fields."""
    for arg_index, arg in enumerate(cmd):
        nul_position = arg.find("\x00")
        if nul_position < 0:
            continue

        detail: dict[str, object] = {
            "argument_index": arg_index,
            "argument_flag": (
                cmd[arg_index - 1]
                if arg_index > 0 and cmd[arg_index - 1].startswith("--")
                else None
            ),
            "position": nul_position,
            "preview": _preview_window(arg, nul_position),
            "hex_window": _hex_window(arg, nul_position),
        }
        for source in input_sources or []:
            source_text = source.get("text", "")
            source_nul_position = source_text.find("\x00")
            if source_nul_position >= 0:
                detail["source"] = source.get("source")
                detail["source_position"] = source_nul_position
                detail["source_preview"] = _preview_window(source_text, source_nul_position)
                detail["source_hex_window"] = _hex_window(source_text, source_nul_position)
                break
        return detail
    return None


def _build_exception_context(
    exc: Exception,
    *,
    cmd: list[str] | None = None,
    input_sources: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Serialize rich exception context for audit/debugging."""
    context: dict[str, object] = {
        "exception_class": type(exc).__name__,
        "exception_args": [repr(arg) for arg in exc.args],
        "traceback": traceback.format_exc(),
    }
    if cmd is not None:
        context["command"] = _render_command_for_audit(cmd)
    if (
        isinstance(exc, ValueError)
        and "embedded null byte" in str(exc).lower()
        and cmd is not None
    ):
        offending_input = _identify_embedded_null_source(cmd, input_sources)
        if offending_input is not None:
            context["offending_input"] = offending_input
    return context


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
    pr_body_sources = [{"source": "parsed_review.summary", "text": parsed_review.summary}]
    for index, finding in enumerate(p2_findings):
        pr_body_sources.append(
            {
                "source": f"finding[{index}].description",
                "text": finding.description,
            }
        )
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
        return {
            "action": "pr",
            "pr_url": None,
            "success": False,
            "error": str(exc),
            "error_context": _build_exception_context(
                exc,
                cmd=cmd,
                input_sources=pr_body_sources,
            ),
            "sanitization_audit": parsed_review.sanitization_audit or None,
        }


def _step_fetch_rebase(push_cwd: Path, base_branch: str) -> dict:
    """Fetch origin/{base_branch} and rebase the worktree onto it.

    Returns ``{"success": True}`` or ``{"success": False, "error": ...}``.
    """
    _deindex_forge_artifacts(push_cwd)
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
            return {"success": False, "error": f"git fetch failed: {err}"}

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
            return {
                "success": False,
                "error": f"rebase onto {base_branch} failed — escalating: {err}",
            }
    except Exception as exc:
        _pr_log.warning("rebase step failed: %s", exc)
        return {
            "success": False,
            "error": f"rebase step failed: {exc}",
            "error_context": _build_exception_context(
                exc,
                cmd=["git", "rebase", f"origin/{base_branch}"],
            ),
        }

    return {"success": True}


def _step_force_push(push_cwd: Path, branch_name: str) -> dict:
    """Force-push the rebased branch to origin.

    Returns ``{"success": True}`` or ``{"success": False, "error": ...}``.
    A missing remote branch is treated as success (already cleaned up).
    """
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
            if "does not match any" in err.lower():
                _pr_log.info(
                    "force-push skipped because remote branch %s is already gone: %s",
                    branch_name,
                    err,
                )
                return {"success": True}
            _pr_log.warning("force-push failed (exit %d): %s", push_proc.returncode, err)
            return {"success": False, "error": f"force-push after rebase failed: {err}"}
    except Exception as exc:
        _pr_log.warning("force-push failed: %s", exc)
        return {
            "success": False,
            "error": f"force-push after rebase failed: {exc}",
            "error_context": _build_exception_context(
                exc,
                cmd=["git", "push", "-f", "origin", branch_name],
            ),
        }

    return {"success": True}


def _step_create_pr(
    config: ForgeConfig,
    task: TaskStory,
    branch_name: str,
    parsed_review: ReviewResult,
    state: CoordinatorState,
) -> dict:
    """Create the GitHub PR (archives story, pushes branch).

    Returns ``{"success": True, "pr_url": ...}`` or
    ``{"success": False, "error": ..., "pr_url": ...}``.
    """
    pr_result = _create_pr(config, task, branch_name, parsed_review, state)
    if not pr_result.get("success"):
        return {"success": False, **pr_result}
    return {"success": True, "pr_url": pr_result["pr_url"]}


def _step_merge(project_root: Path, pr_url: str, merge_strategy: str) -> dict:
    """Execute ``gh pr merge --auto --{strategy}``.

    Returns ``{"success": True}`` or
    ``{"success": False, "error": ..., "retryable": bool}``.
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
        return {
            "success": False,
            "error": f"gh pr merge failed: {err}",
            "retryable": merge_retry_error in err.lower(),
        }

    return {"success": True}


def _step_await_merge_state(project_root: Path, pr_url: str) -> dict:
    """Check whether GitHub merged the PR immediately or queued it for checks.

    Returns ``{"success": True, "merge_queued": bool, "auto_merge_queued": bool}``
    or ``{"success": False, "error": ...}``.
    """
    try:
        state_proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state", "-q", ".state"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    except Exception as exc:
        _pr_log.warning("gh pr view failed after auto-merge queue: %s", exc)
        return {
            "success": False,
            "error": f"gh pr view failed after auto-merge queue: {exc}",
            "error_context": _build_exception_context(
                exc,
                cmd=["gh", "pr", "view", pr_url, "--json", "state", "-q", ".state"],
            ),
        }

    if state_proc.returncode != 0:
        state_err = state_proc.stderr.strip() or state_proc.stdout.strip()
        _pr_log.warning(
            "gh pr view failed after auto-merge queue (exit %d): %s",
            state_proc.returncode,
            state_err,
        )
        return {
            "success": False,
            "error": f"gh pr view failed after auto-merge queue: {state_err}",
        }

    pr_state = state_proc.stdout.strip()
    queued = pr_state != "MERGED"
    return {"success": True, "merge_queued": queued, "auto_merge_queued": queued}


def _step_cleanup(
    project_root: Path,
    worktree_path: Path,
    branch_name: str,
    base_branch: str,
    *,
    delete_remote_branch: bool,
) -> None:
    """Best-effort local cleanup after a successful remote PR merge."""
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=60,
        )
        subprocess.run(
            ["git", "merge", "--ff-only", f"origin/{base_branch}"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    except Exception as exc:
        _pr_log.warning("local base_branch fast-forward failed (non-fatal): %s", exc)

    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    except Exception as exc:
        _pr_log.warning("worktree cleanup failed (non-fatal): %s", exc)

    try:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            capture_output=True,
            text=True,
            cwd=str(project_root),
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
            cwd=str(project_root),
            timeout=60,
        )
    except Exception as exc:
        _pr_log.warning("remote branch cleanup failed (non-fatal): %s", exc)


def _merge_pr(
    config: ForgeConfig,
    task: TaskStory,
    branch_name: str,
    parsed_review: ReviewResult,
    state: CoordinatorState,
) -> dict:
    """Create a PR and immediately merge it via gh pr merge.

    Sequence:
    1. Check if PR is already merged (fast-exit).
    2. Fetch + rebase onto latest origin/{base_branch} (escalate on conflict).
    3. Force-push rebased branch so _step_create_pr's push is a fast-forward.
    4. Call _step_create_pr() to archive story, push, and open the PR (once only).
    5. Merge via ``gh pr merge --auto --{strategy}`` from the project root.
    6. Confirm PR state (MERGED or queued for auto-merge).
    7. Best-effort local cleanup.

    Each step's outcome is persisted to merge_state.yaml so a crash mid-merge
    can be resumed from the last committed step.  Each step logs its outcome via
    _pr_log so failures are independently auditable in the run log.

    Returns a result dict with keys: action, pr_url, merged, success, error.
    Never raises.
    """
    base_branch = config.workspace.base_branch
    merge_strategy = config.workspace.merge_strategy
    worktree_dir = config.workspace.path_pattern.format(slug=task.slug)
    worktree_path = config.project_root / worktree_dir
    push_cwd = worktree_path if worktree_path.is_dir() else config.project_root

    def _fail(error: str, *, merge_state: MergeStepState, detail: dict | None = None) -> dict:
        merge_state.error = error
        save_merge_state(worktree_path, merge_state)
        next_action = (
            "Inspect the existing PR and retry merge automation."
            if merge_state.pr_url
            else (
                "Retry PR creation after sanitizing review text, or run "
                "`gh pr create` manually from the feature branch."
            )
        )
        return {
            "action": "merge-pr",
            "pr_url": merge_state.pr_url,
            "merged": False,
            "merge_queued": False,
            "auto_merge_queued": False,
            "success": False,
            "error": error,
            "branch": branch_name,
            "strand_state": {
                "branch": branch_name,
                "worktree_path": str(worktree_path),
                "pr_url": merge_state.pr_url,
                "next_action": next_action,
            },
            **(detail or {}),
        }

    def _complete_step(merge_state: MergeStepState, step: str, **log_kwargs: object) -> None:
        merge_state.completed_steps.append(step)
        save_merge_state(worktree_path, merge_state)
        _pr_log.info("merge step %s completed: %s", step, log_kwargs)

    # Load any persisted step state (enables crash-resume).
    merge_state = load_merge_state(worktree_path)

    # Check if the PR is already merged (always runs — external state may have changed).
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
                _pr_log.info(
                    "PR already merged for branch %s; skipping PR recreation and merge retry: %s",
                    branch_name,
                    pr_url,
                )
                if "cleanup" not in merge_state.completed_steps:
                    _step_cleanup(
                        config.project_root,
                        worktree_path,
                        branch_name,
                        base_branch,
                        delete_remote_branch=False,
                    )
                    _complete_step(merge_state, "cleanup", pr_url=pr_url)
                delete_merge_state(worktree_path)
                return {
                    "action": "merge-pr",
                    "pr_url": pr_url,
                    "merged": True,
                    "merge_queued": False,
                    "auto_merge_queued": False,
                    "success": True,
                    "error": None,
                }
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

    # Retry loop: handles transient "base branch was modified" errors from GitHub.
    for attempt in range(MAX_MERGE_RETRIES):
        # fetch + rebase and force-push are only needed before the merge command runs.
        # If merge already completed (resuming after a crash post-merge), skip them:
        # force-pushing after auto-merge is queued can dismiss approvals and cancel it.
        if "merge" not in merge_state.completed_steps:
            fetch_rebase_result = _step_fetch_rebase(push_cwd, base_branch)
            _pr_log.info(
                "merge step fetch_rebase (attempt %d): success=%s error=%s",
                attempt,
                fetch_rebase_result["success"],
                fetch_rebase_result.get("error"),
            )
            if not fetch_rebase_result["success"]:
                return _fail(
                    fetch_rebase_result["error"],
                    merge_state=merge_state,
                    detail={
                        "error_context": fetch_rebase_result.get("error_context"),
                    },
                )

            force_push_result = _step_force_push(push_cwd, branch_name)
            _pr_log.info(
                "merge step force_push (attempt %d): success=%s error=%s",
                attempt,
                force_push_result["success"],
                force_push_result.get("error"),
            )
            if not force_push_result["success"]:
                return _fail(
                    force_push_result["error"],
                    merge_state=merge_state,
                    detail={
                        "error_context": force_push_result.get("error_context"),
                    },
                )

        # create PR — only once; skip on resume if already completed.
        if "create_pr" not in merge_state.completed_steps:
            create_result = _step_create_pr(config, task, branch_name, parsed_review, state)
            if not create_result["success"]:
                return _fail(
                    create_result["error"],
                    merge_state=merge_state,
                    detail={
                        key: create_result.get(key)
                        for key in ("error_context", "sanitization_audit")
                        if create_result.get(key) is not None
                    },
                )
            if create_result["pr_url"] is None:
                # Zero-delta branch: no commits ahead of base, nothing to merge.
                _log(
                    f"  Skipping merge: branch {branch_name} has no commits ahead of "
                    f"origin/{base_branch}"
                )
                delete_merge_state(worktree_path)
                return {
                    "action": "merge-pr",
                    "pr_url": None,
                    "merged": False,
                    "merge_queued": False,
                    "auto_merge_queued": False,
                    "success": True,
                    "error": None,
                    "skipped": True,
                    "skip_reason": "zero-delta branch",
                }
            merge_state.pr_url = create_result["pr_url"]
            _complete_step(merge_state, "create_pr", pr_url=merge_state.pr_url)

        # Scrub forge artifacts before merge consumes the rewritten commit graph.
        _deindex_forge_artifacts(push_cwd)

        # merge — skip on resume if already completed.
        if "merge" not in merge_state.completed_steps:
            merge_result = _step_merge(config.project_root, merge_state.pr_url, merge_strategy)
            _pr_log.info(
                "merge step merge (attempt %d): success=%s error=%s",
                attempt,
                merge_result["success"],
                merge_result.get("error"),
            )
            if not merge_result["success"]:
                if merge_result.get("retryable") and attempt < MAX_MERGE_RETRIES - 1:
                    _pr_log.warning(
                        "base branch changed during PR merge attempt %d/%d; retrying",
                        attempt + 1,
                        MAX_MERGE_RETRIES,
                    )
                    continue
                return _fail(
                    merge_result["error"],
                    merge_state=merge_state,
                    detail={
                        "error_context": merge_result.get("error_context"),
                    },
                )
            _complete_step(merge_state, "merge")

        # Determine whether GitHub merged immediately or queued for checks.
        if "await_merge_state" not in merge_state.completed_steps:
            await_result = _step_await_merge_state(config.project_root, merge_state.pr_url)
            if not await_result["success"]:
                return _fail(
                    await_result["error"],
                    merge_state=merge_state,
                    detail={
                        "error_context": await_result.get("error_context"),
                    },
                )
            merge_state.merge_queued = await_result["merge_queued"]
            merge_state.auto_merge_queued = await_result["auto_merge_queued"]
            _complete_step(
                merge_state,
                "await_merge_state",
                merge_queued=merge_state.merge_queued,
                pr_url=merge_state.pr_url,
            )

        break  # success — exit retry loop

    merge_queued = merge_state.merge_queued
    auto_merge_queued = merge_state.auto_merge_queued

    if merge_queued:
        _log(f"  ✓ PR queued for auto-merge: {merge_state.pr_url}")
    else:
        _log(f"  ✓ PR merged: {merge_state.pr_url}")

    # Sync local state and clean up the merged feature worktree/branch.
    # Preserve the remote branch when GitHub only queued auto-merge; branch
    # protection still needs that ref until the hosted merge completes.
    if "cleanup" not in merge_state.completed_steps:
        _step_cleanup(
            config.project_root,
            worktree_path,
            branch_name,
            base_branch,
            delete_remote_branch=not merge_queued,
        )
        _complete_step(merge_state, "cleanup", pr_url=merge_state.pr_url)

    delete_merge_state(worktree_path)

    return {
        "action": "merge-pr",
        "pr_url": merge_state.pr_url,
        "merged": not merge_queued,
        "merge_queued": merge_queued,
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


def land_story(
    config: ForgeConfig,
    task: TaskStory,
    branch_name: str,
    workspace_path: Path,
    parsed_review: "ReviewResult | None",
    state: CoordinatorState,
    effective_on_approve: str,
    *,
    logger: "StructuredLogger | None" = None,
    run_id: str = "",
) -> "tuple[dict, str]":
    """Execute the actual merge/landing for an approved story.

    Returns ``(merge_info, landing_status)``.

    The caller is responsible for acquiring ``integration_lock`` when needed
    (sprint scheduler path).  Single-story ``run_task`` calls this directly
    without a lock because there is only one worker.

    ``effective_on_approve`` must be ``"merge"`` or ``"merge-pr"``; callers
    should not invoke this function for ``"pr"`` or ``"none"`` modes.
    """
    if effective_on_approve == "merge":
        # Pre-merge cleanup: commit any tracked dirty files in the worktree
        status_ok, status_out = _run_shell("git status --porcelain", workspace_path)
        if status_ok:
            tracked_dirty = _parse_dirty_files(status_out)
            untracked_files = [
                line[3:].strip() for line in status_out.splitlines() if line.startswith("?? ")
            ]
            if tracked_dirty:
                quoted = " ".join(shlex.quote(path) for path in tracked_dirty)
                add_ok, add_out = _run_shell(f"git add -- {quoted}", workspace_path)
                if add_ok:
                    commit_ok, commit_out = _run_shell(
                        'git commit -m "chore: commit remaining worktree changes before merge"',
                        workspace_path,
                    )
                    if commit_ok:
                        _log(
                            "  Auto-committed tracked worktree changes before merge: "
                            + ", ".join(tracked_dirty)
                        )
                    else:
                        _log(f"Warning: pre-merge cleanup commit failed: {commit_out}")
                else:
                    _log(f"Warning: pre-merge cleanup git add failed: {add_out}")
            if untracked_files:
                _log(
                    "Warning: untracked files left in worktree before merge (not auto-added): "
                    + ", ".join(untracked_files)
                )
        else:
            _log(f"Warning: could not inspect worktree before merge: {status_out}")

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

        landing_status = "landed" if merge_info["merged"] else "failed"
        return merge_info, landing_status

    elif effective_on_approve == "merge-pr":
        if parsed_review is None:
            _log("WARN: land_story called for merge-pr but parsed_review is None — skipping")
            return {"merged": False, "error": "no review result available"}, "failed"

        merge_info = _merge_pr(config, task, branch_name, parsed_review, state)
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

        if not merge_info["merged"] and not merge_info.get("merge_queued"):
            landing_status = "failed"
        elif merge_info.get("merge_queued"):
            landing_status = "pending_integration"
        else:
            landing_status = "landed"
        return merge_info, landing_status

    else:
        raise ValueError(
            f"land_story called for unsupported effective_on_approve={effective_on_approve!r}; "
            "only 'merge' and 'merge-pr' are valid"
        )


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
    """Set DONE, mark landing pending, log, notify, return CoordinatorResult.

    Merge/landing is deferred: callers (run_task for single-story, _attempt_integration
    for sprint) invoke land_story() after this returns.

    Pass logger=None to suppress phase_end logger events (interactive paths).
    Pass logger=logger to emit them (non-interactive path).
    """
    state.phase = Phase.DONE
    merge_info: dict | None = None
    merge_suffix = ""
    landing_status: str | None = None

    # Resolve effective on_approve: CLI --auto-merge flag forces "merge"
    effective_on_approve = "merge" if auto_merge else config.workspace.on_approve

    if effective_on_approve in ("merge", "merge-pr"):
        # Defer to land_story() — no git operations here.
        merge_info = {"action": effective_on_approve, "pending": True}
        landing_status = "pending_integration"
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
        landing_status=landing_status,
    )
