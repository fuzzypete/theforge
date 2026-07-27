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
    # GraphQL StatusContext state for a hard-errored legacy status.
    "error",
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


def _resolve_owner_repo(project_root: Path) -> str:
    repo = _gh_json(project_root, ["repo", "view", "--json", "nameWithOwner"])
    owner_repo = repo["nameWithOwner"] if isinstance(repo, dict) else None
    if not isinstance(owner_repo, str) or not owner_repo:
        raise RuntimeError("Unable to resolve GitHub repository owner/name")
    return owner_repo


def _required_check_contexts(
    project_root: Path, owner_repo: str, base_branch: str
) -> list[str] | None:
    """Required status-check contexts for ``base_branch``.

    Returns ``None`` when branch protection (or its required-checks block) does
    not exist, which callers must treat as "unknown", not as "none failing".
    """
    try:
        raw = _gh_json(
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
            return None
        raise
    return [c for c in raw if isinstance(c, str)] if isinstance(raw, list) else []


def _normalize_rollup(rollup: object) -> tuple[list[dict], list[dict]]:
    """Split a ``statusCheckRollup`` payload into check-run / legacy-status shapes.

    The rollup comes from GraphQL, so its enums are upper-case
    (``COMPLETED``/``FAILURE``); the REST-shaped summarizer expects the
    lower-case spellings. Normalizing here keeps a single conclusion table.
    """

    def _lower(value: object) -> str | None:
        return value.lower() if isinstance(value, str) else None

    check_runs: list[dict] = []
    statuses: list[dict] = []
    for entry in rollup if isinstance(rollup, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        context = entry.get("context")
        if entry.get("__typename") == "StatusContext" or (
            not isinstance(name, str) and isinstance(context, str)
        ):
            if isinstance(context, str):
                statuses.append({"context": context, "state": _lower(entry.get("state"))})
        elif isinstance(name, str):
            check_runs.append(
                {
                    "name": name,
                    "status": _lower(entry.get("status")),
                    "conclusion": _lower(entry.get("conclusion")),
                }
            )
    return check_runs, statuses


def failing_required_pr_checks(project_root: Path, pr_url: str, base_branch: str) -> list[str]:
    """Names of ``pr_url``'s required checks that have terminally failed.

    Returns an empty list when nothing is decided-red *and* when the required
    check set or the PR's rollup cannot be resolved: an un-answerable probe must
    leave the caller waiting rather than abandon a PR on missing information.
    """
    try:
        owner_repo = _resolve_owner_repo(project_root)
        required_checks = _required_check_contexts(project_root, owner_repo, base_branch)
        if not required_checks:
            return []
        rollup = _gh_json(
            project_root,
            ["pr", "view", pr_url, "--json", "statusCheckRollup", "--jq", ".statusCheckRollup"],
        )
    except Exception as exc:  # gh missing, network flake, unparseable payload
        log.debug("Unable to resolve required check status for %s: %s", pr_url, exc)
        return []

    check_runs, statuses = _normalize_rollup(rollup)
    _, failing, _ = _summarize_required_checks(required_checks, check_runs, statuses)
    return failing


def poll_required_checks(project_root: Path, base_branch: str, timeout_seconds: int) -> dict:
    owner_repo = _resolve_owner_repo(project_root)

    sha_raw = _gh_text(
        project_root, ["api", f"repos/{owner_repo}/branches/{base_branch}", "--jq", ".commit.sha"]
    )
    sha = json.loads(sha_raw) if sha_raw.startswith('"') else sha_raw
    required_checks = _required_check_contexts(project_root, owner_repo, base_branch)
    if required_checks is None:
        return {
            "status": "skipped",
            "sha": sha,
            "failing_checks": [],
            "message": (
                f"Required status checks not configured for {base_branch}; "
                f"skipping CI gate for {sha}."
            ),
        }

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
