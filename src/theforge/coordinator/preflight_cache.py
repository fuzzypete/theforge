"""Helpers for capturing and validating cached preflight git state."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import util as _cu

if TYPE_CHECKING:
    from theforge.config import ForgeConfig

    from .state import CoordinatorState


def _git_rev_parse(cwd: Path, rev: str) -> str | None:
    ok, out = _cu._run_shell(f"git rev-parse {rev}", cwd)
    value = out.strip() if ok else ""
    return value or None


def _story_content_hash(story_content: str) -> str:
    return hashlib.sha256(story_content.encode("utf-8")).hexdigest()


def capture_preflight_cache_snapshot(
    *,
    config: "ForgeConfig",
    workspace_path: Path,
    story_content: str,
) -> dict[str, str]:
    """Return the fingerprint that makes a preflight verdict reusable.

    A preflight verdict is a judgement about a specific story text evaluated
    against a specific tree state, so both must be captured for the cached
    verdict to be validly reusable.
    """
    snapshot: dict[str, str] = {}

    worktree_head = _git_rev_parse(workspace_path, "HEAD")
    if worktree_head is not None:
        snapshot["worktree_head"] = worktree_head

    base_branch = config.workspace.base_branch
    snapshot["evaluation_base_branch"] = base_branch
    base_branch_head = _git_rev_parse(config.project_root, base_branch)
    if base_branch_head is not None:
        snapshot["evaluation_base_branch_head"] = base_branch_head

    snapshot["story_content_hash"] = _story_content_hash(story_content)

    return snapshot


def validate_preflight_cache(
    cached_state: "CoordinatorState",
    *,
    config: "ForgeConfig",
    workspace_path: Path,
    story_content: str,
) -> tuple[bool, dict[str, Any]]:
    """Return whether cached preflight can be reused for the current state."""
    snapshot = dict(getattr(cached_state, "preflight_cache_snapshot", {}) or {})
    current_worktree_head = _git_rev_parse(workspace_path, "HEAD")
    current_base_branch = config.workspace.base_branch
    current_base_branch_head = _git_rev_parse(config.project_root, current_base_branch)
    current_story_content_hash = _story_content_hash(story_content)

    validation: dict[str, Any] = {
        "cached": bool(snapshot),
        "cached_worktree_head": snapshot.get("worktree_head"),
        "current_worktree_head": current_worktree_head,
        "cached_evaluation_base_branch": snapshot.get("evaluation_base_branch"),
        "current_evaluation_base_branch": current_base_branch,
        "cached_evaluation_base_branch_head": snapshot.get("evaluation_base_branch_head"),
        "current_evaluation_base_branch_head": current_base_branch_head,
        "cached_story_content_hash": snapshot.get("story_content_hash"),
        "current_story_content_hash": current_story_content_hash,
    }

    if not snapshot:
        validation["status"] = "invalidated"
        validation["reason"] = "missing_git_state_snapshot"
        return False, validation

    required = (
        snapshot.get("worktree_head"),
        snapshot.get("evaluation_base_branch"),
        snapshot.get("evaluation_base_branch_head"),
        snapshot.get("story_content_hash"),
        current_worktree_head,
        current_base_branch_head,
    )
    if any(value in (None, "") for value in required):
        validation["status"] = "invalidated"
        validation["reason"] = "git_state_unavailable"
        return False, validation

    if snapshot["evaluation_base_branch"] != current_base_branch:
        validation["status"] = "invalidated"
        validation["reason"] = "evaluation_base_branch_changed"
        return False, validation

    if snapshot["worktree_head"] != current_worktree_head:
        validation["status"] = "invalidated"
        validation["reason"] = "worktree_head_changed"
        return False, validation

    if snapshot["evaluation_base_branch_head"] != current_base_branch_head:
        validation["status"] = "invalidated"
        validation["reason"] = "evaluation_base_branch_head_changed"
        return False, validation

    if snapshot["story_content_hash"] != current_story_content_hash:
        validation["status"] = "invalidated"
        validation["reason"] = "story_content_changed"
        return False, validation

    validation["status"] = "accepted"
    validation["reason"] = "git_state_match"
    return True, validation


def apply_cached_preflight_state(
    state: "CoordinatorState",
    cached_state: "CoordinatorState",
) -> None:
    """Copy preflight outputs from cached_state onto the live coordinator state."""
    state.preflight_verdict = cached_state.preflight_verdict
    state.preflight_reason = cached_state.preflight_reason
    state.preflight_complexity = cached_state.preflight_complexity
    state.preflight_complexity_score = cached_state.preflight_complexity_score
    state.preflight_implementation_complexity_score = (
        cached_state.preflight_implementation_complexity_score
    )
    state.preflight_validation_complexity_score = (
        cached_state.preflight_validation_complexity_score
    )
    state.preflight_complexity_projection = cached_state.preflight_complexity_projection
    state.preflight_complexity_evidence = list(cached_state.preflight_complexity_evidence or [])
    state.preflight_sufficiency = cached_state.preflight_sufficiency
    state.preflight_work_type = cached_state.preflight_work_type
    state.preflight_domains = list(cached_state.preflight_domains or [])
    state.preflight_contract_change = cached_state.preflight_contract_change
    state.preflight_bundle_candidate = cached_state.preflight_bundle_candidate
    state.preflight_batch_group = cached_state.preflight_batch_group
    state.preflight_warnings = list(cached_state.preflight_warnings or [])
    state.preflight_likely_files = (
        None
        if cached_state.preflight_likely_files is None
        else list(cached_state.preflight_likely_files)
    )
    state.preflight_result = cached_state.preflight_result
    state.preflight_duration_s = cached_state.preflight_duration_s
    state.preflight_cached = True
    state.preflight_cached_original_verdict = cached_state.preflight_verdict
    state.preflight_cached_from_run_id = getattr(cached_state, "run_id", None)
    state.preflight_criteria_checked = list(
        getattr(cached_state, "preflight_criteria_checked", None) or []
    )
    state.preflight_cache_snapshot = dict(
        getattr(cached_state, "preflight_cache_snapshot", {}) or {}
    )
