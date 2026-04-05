"""Polling helpers for required branch-protection CI checks."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 15
_PASS_CONCLUSIONS = {"success", "neutral"}
_FAIL_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}
_PENDING_STATUSES = {"queued", "in_progress", "pending", "waiting", "requested"}


def _gh_json(project_root: Path, args: list[str]) -> object:
    proc = subprocess.run(
        ["gh", *args],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(err or f"gh {' '.join(args)} failed")
    stdout = proc.stdout.strip()
    return json.loads(stdout) if stdout else None


def _gh_text(project_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["gh", *args],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(err or f"gh {' '.join(args)} failed")
    return proc.stdout.strip()


def _summarize_required_checks(
    required_checks: list[str], check_runs: object, statuses: object
) -> tuple[str, list[str], list[str]]:
    run_map: dict[str, tuple[str | None, str | None]] = {}
    for run in check_runs if isinstance(check_runs, list) else []:
        if not isinstance(run, dict):
            continue
        name = run.get("name")
        if isinstance(name, str):
            run_map[name] = (run.get("status"), run.get("conclusion"))

    status_map: dict[str, str] = {}
    if isinstance(statuses, list):
        for status in statuses:
            if not isinstance(status, dict):
                continue
            context = status.get("context")
            state = status.get("state")
            if isinstance(context, str) and isinstance(state, str):
                status_map[context] = state

    passing: list[str] = []
    failing: list[str] = []
    pending: list[str] = []
    for name in required_checks:
        run_status, run_conclusion = run_map.get(name, (None, None))
        legacy_state = status_map.get(name)
        if run_conclusion in _PASS_CONCLUSIONS or legacy_state in _PASS_CONCLUSIONS:
            passing.append(name)
            continue
        if run_conclusion in _FAIL_CONCLUSIONS or legacy_state in _FAIL_CONCLUSIONS:
            failing.append(name)
            continue
        if (
            run_status in _PENDING_STATUSES
            or legacy_state in _PENDING_STATUSES
            or name not in run_map
            and name not in status_map
        ):
            pending.append(name)
            continue
        pending.append(name)
    return (
        "pass" if len(passing) == len(required_checks) else "fail" if failing else "pending",
        failing,
        pending,
    )


def poll_required_checks(project_root: Path, base_branch: str, timeout_seconds: int) -> dict:
    repo = _gh_json(project_root, ["repo", "view", "--json", "nameWithOwner"])
    owner_repo = repo["nameWithOwner"] if isinstance(repo, dict) else None
    if not isinstance(owner_repo, str) or not owner_repo:
        raise RuntimeError("Unable to resolve GitHub repository owner/name")

    sha_raw = _gh_text(
        project_root, ["api", f"repos/{owner_repo}/branches/{base_branch}", "--jq", ".commit.sha"]
    )
    sha = json.loads(sha_raw) if sha_raw.startswith('"') else sha_raw
    try:
        required_checks_raw = _gh_json(
            project_root,
            [
                "api",
                f"repos/{owner_repo}/branches/{base_branch}/protection/required_status_checks",
                "--jq",
                "[.checks[].context]",
            ],
        )
    except RuntimeError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return {
                "status": "skipped",
                "sha": sha,
                "failing_checks": [],
                "message": (
                    f"Required status checks not configured for {base_branch}; "
                    f"skipping CI gate for {sha}."
                ),
            }
        raise

    required_checks = (
        [c for c in required_checks_raw if isinstance(c, str)]
        if isinstance(required_checks_raw, list)
        else []
    )
    if not required_checks:
        return {
            "status": "skipped",
            "sha": sha,
            "failing_checks": [],
            "message": (
                f"No required status checks configured for {base_branch}; "
                f"skipping CI gate for {sha}."
            ),
        }

    deadline = time.monotonic() + timeout_seconds
    while True:
        check_runs = _gh_json(
            project_root,
            ["api", f"repos/{owner_repo}/commits/{sha}/check-runs", "--jq", ".check_runs"],
        )
        statuses = _gh_json(
            project_root,
            ["api", f"repos/{owner_repo}/commits/{sha}/status", "--jq", ".statuses"],
        )
        summary, failing, pending = _summarize_required_checks(
            required_checks, check_runs, statuses
        )
        log.debug(
            "CI poll sha=%s required=%s failing=%s pending=%s",
            sha,
            required_checks,
            failing,
            pending,
        )
        if summary == "pass":
            return {
                "status": "pass",
                "sha": sha,
                "failing_checks": [],
                "message": f"Required CI checks passed for {sha}.",
            }
        if summary == "fail":
            return {
                "status": "fail",
                "sha": sha,
                "failing_checks": failing,
                "message": f"Required CI checks failed for {sha}: {', '.join(failing)}.",
            }
        if time.monotonic() >= deadline:
            pending_checks = ", ".join(pending)
            return {
                "status": "timeout",
                "sha": sha,
                "failing_checks": [],
                "message": (
                    "Timed out after "
                    f"{timeout_seconds}s waiting for required CI checks on {sha}: "
                    f"{pending_checks}."
                ),
            }
        time.sleep(_POLL_INTERVAL_SECONDS)
