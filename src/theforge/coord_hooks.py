"""Lifecycle hook execution and payload builders.

run_hook() is the sole execution point. It serialises the payload dict as JSON
onto stdin, invokes the command, and returns a HookResult.  It never raises.

Payload builders accept the objects in scope at each call site and return plain
dicts suitable for JSON serialisation.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ForgeConfig
    from .coord_logging import StructuredLogger
    from .coord_state import CoordinatorResult, CoordinatorState
    from .task import TaskSpec


@dataclass
class HookResult:
    """Result from a single hook invocation."""

    success: bool
    exit_code: int  # 0 = success or skipped; non-zero = failure
    output: str  # combined stdout + stderr
    duration_s: float


def run_hook(
    command: str,
    payload: dict,
    timeout: int,
    label: str,
    logger: StructuredLogger | None = None,
) -> HookResult:
    """Execute a lifecycle hook command with payload on stdin.

    Always returns a HookResult — never raises.  Non-zero exit is logged as a
    warning but does NOT fail the forge run (callers abort only for pre_run).

    If the executable portion of the command does not exist or is not
    executable, the hook is silently skipped (debug log only).
    """
    _start = time.monotonic()

    # Executable check: extract first token and verify it's runnable when it
    # looks like a file path (contains a slash).
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Malformed command string (e.g. unmatched quote) — silently skip.
        if logger:
            logger._safe_emit(
                "hook",
                hook=label,
                success=True,
                exit_code=0,
                duration_s=0.0,
                skipped=True,
                reason="malformed_command",
            )
        return HookResult(success=True, exit_code=0, output="", duration_s=0.0)
    exe = tokens[0] if tokens else command
    if os.sep in exe or exe.startswith("."):
        if not os.path.exists(exe):
            if logger:
                logger._safe_emit(
                    "hook",
                    hook=label,
                    success=True,
                    exit_code=0,
                    duration_s=0.0,
                    skipped=True,
                    reason="file_not_found",
                )
            return HookResult(success=True, exit_code=0, output="", duration_s=0.0)
        if not os.access(exe, os.X_OK):
            if logger:
                logger._safe_emit(
                    "hook",
                    hook=label,
                    success=True,
                    exit_code=0,
                    duration_s=0.0,
                    skipped=True,
                    reason="not_executable",
                )
            return HookResult(success=True, exit_code=0, output="", duration_s=0.0)

    payload_json = json.dumps(payload)
    try:
        proc = subprocess.run(
            shlex.split(command),
            input=payload_json,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration_s = round(time.monotonic() - _start, 3)
        output = (proc.stdout or "") + (proc.stderr or "")
        success = proc.returncode == 0
        if not success:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Hook %r exited %d: %s", label, proc.returncode, output[:200]
            )
        if logger:
            logger._safe_emit(
                "hook",
                hook=label,
                success=success,
                exit_code=proc.returncode,
                duration_s=duration_s,
            )
        return HookResult(
            success=success,
            exit_code=proc.returncode,
            output=output,
            duration_s=duration_s,
        )
    except subprocess.TimeoutExpired:
        duration_s = round(time.monotonic() - _start, 3)
        import logging as _logging

        _logging.getLogger(__name__).warning("Hook %r timed out after %ds", label, timeout)
        if logger:
            logger._safe_emit(
                "hook",
                hook=label,
                success=False,
                exit_code=-1,
                duration_s=duration_s,
                error="timeout",
            )
        return HookResult(success=False, exit_code=-1, output="", duration_s=duration_s)
    except Exception as exc:
        duration_s = round(time.monotonic() - _start, 3)
        import logging as _logging

        _logging.getLogger(__name__).warning("Hook %r failed: %s", label, exc)
        if logger:
            logger._safe_emit(
                "hook",
                hook=label,
                success=False,
                exit_code=-1,
                duration_s=duration_s,
                error=str(exc),
            )
        return HookResult(success=False, exit_code=-1, output="", duration_s=duration_s)


# ── Payload builders ─────────────────────────────────────────────────


def build_pre_run_payload(
    task: TaskSpec,
    run_id: str,
    config: ForgeConfig,
) -> dict:
    """Minimal payload for pre_run hook."""
    return {
        "event": "pre_run",
        "project": config.project,
        "slug": task.slug,
        "spec": str(task.spec_path),
        "run_id": run_id,
    }


def build_post_run_payload(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    result: CoordinatorResult,
    run_id: str,
    duration_seconds: float,
) -> dict:
    """Full payload for post_run hook, built from coordinator state."""
    # Determine verdict and summary from last review result
    verdict: str
    summary: str
    findings: list[dict] = []

    # Default verdict before checking review_results (which overrides below).
    verdict = "APPROVE" if result.success else "ESCALATE"

    if state.review_results:
        last_review = state.review_results[-1]
        verdict = last_review.verdict
        summary = last_review.summary
        findings = [
            {
                "severity": f.severity,
                "file": f.file,
                "line": f.line,
                "description": f.description,
                "suggestion": f.suggestion,
            }
            for f in last_review.findings
        ]
    else:
        summary = result.message or ""

    # Review pool names from the last cycle metadata (or config)
    review_pool: list[str] = [p.name for p in config.review_pool]
    review_pool_failed: list[str] = []
    if state.review_cycle_metadata:
        last_meta = state.review_cycle_metadata[-1]
        review_pool = last_meta.successful + last_meta.failed
        review_pool_failed = last_meta.failed

    outcome = "done" if result.success else "escalate"
    branch = state.branch_name or config.workspace.branch_pattern.format(slug=task.slug)

    return {
        "event": "post_run",
        "project": config.project,
        "slug": task.slug,
        "spec": str(task.spec_path),
        "branch": branch,
        "run_id": run_id,
        "outcome": outcome,
        "verdict": verdict,
        "summary": summary,
        "cycles": state.review_cycle,
        "dev_iterations": len(state.dev_results),
        "total_cost_usd": round(state.total_cost, 4),
        "duration_seconds": round(duration_seconds, 1),
        "findings": findings,
        "gate_decisions": list(state.gate_decisions),
        "review_pool": review_pool,
        "review_pool_failed": review_pool_failed,
    }


def build_post_merge_payload(
    slug: str,
    branch: str,
    run_id: str,
    config: ForgeConfig,
) -> dict:
    """Payload for post_merge hook."""
    return {
        "event": "post_merge",
        "project": config.project,
        "slug": slug,
        "branch": branch,
        "merged_to": config.workspace.base_branch,
        "run_id": run_id,
    }


def build_post_sprint_payload(
    sprint_name: str,
    stories: list[dict],
    run_id: str,
    config: ForgeConfig,
    total_cost_usd: float = 0.0,
    duration_seconds: float = 0.0,
) -> dict:
    """Payload for post_sprint hook."""
    return {
        "event": "post_sprint",
        "project": config.project,
        "sprint": sprint_name,
        "run_id": run_id,
        "total_cost_usd": round(total_cost_usd, 4),
        "duration_seconds": round(duration_seconds, 1),
        "stories": stories,
    }
