"""GitHub Agentic Workflows (gh-aw) runner.

Dispatches a compiled agentic workflow on GitHub Actions via
`gh workflow run` (a workflow_dispatch event), correlates the run through a
unique dispatch id embedded in the run name, polls the run to completion,
and collects the run's artifacts into an AgentResult.

Spike backend for ADR-0004 (execution substrate): TheForge decides
whether/what/who/when; GitHub executes. The local process only dispatches
and polls — agent execution, sandboxing, and network isolation happen on
the Actions runner, so this runner does not wrap itself in the local
workspace sandbox.

Budget semantics degrade on this transport: engine spend is accounted in
Actions minutes + engine units, not direct API dollars, so cost_usd is
None. Timing evidence for the ADR is recorded in AgentResult.raw["timing"].
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from theforge.agent_types import AgentResult
from theforge.log_util import _log_line
from theforge.task.handoff_parser import ParseError, extract_dev_handoff
from theforge.workspace_env import build_workspace_env

from ..config import ModelProfile

# Compiled workflow file the coordinator dispatches. The lock file must exist
# on the ref being dispatched (gh-aw compiles .md → .lock.yml).
_DEFAULT_WORKFLOW = "forge-dev-ghaw.lock.yml"
_WORKFLOW_ENV_VAR = "THEFORGE_GHAW_WORKFLOW"

# workflow_dispatch caps the total input payload at 65535 characters; leave
# headroom for the non-prompt inputs.
_MAX_PROMPT_CHARS = 60_000

# Poll cadence for run discovery and completion. Env-overridable so lifecycle
# tests against the fake gh binary do not sleep for real.
_POLL_ENV_VAR = "THEFORGE_GHAW_POLL_SECONDS"
_DEFAULT_POLL_SECONDS = 10.0
# How long to wait for the dispatched run to appear in `gh run list` before
# concluding the dispatch was lost.
_RUN_DISCOVERY_TIMEOUT_SECONDS = 180.0

# Cap on artifact text folded into AgentResult.output — keeps trace files and
# memory bounded while preserving capture fidelity for typical agent logs.
_MAX_ARTIFACT_TEXT_BYTES = 2_000_000
_ARTIFACT_TEXT_SUFFIXES = (".log", ".txt", ".md", ".json", ".jsonl")


def _log(msg: str) -> None:
    _log_line("[forge]", msg)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL, LogLevel  # noqa: PLC0415

    if _LOG_LEVEL >= LogLevel.VERBOSE:
        _log_line("[forge]", msg)


def _try_parse_handoff(output: str) -> dict | None:
    """Best-effort extraction of <forge_handoff> from agent output. Logs on parse error."""
    try:
        return extract_dev_handoff(output)
    except ParseError as exc:
        _log_verbose(f"  handoff parse error (non-fatal): {exc}")
        return None


def _poll_seconds() -> float:
    raw = os.environ.get(_POLL_ENV_VAR)
    if raw:
        try:
            return max(0.01, float(raw))
        except ValueError:
            pass
    return _DEFAULT_POLL_SECONDS


def workflow_file() -> str:
    """Return the workflow lock file to dispatch (env-overridable)."""
    return os.environ.get(_WORKFLOW_ENV_VAR, _DEFAULT_WORKFLOW)


# ── Argv builders (pure — exercised by tests/contract/test_ghaw_cli_contract.py) ──


def build_argv(
    *,
    workflow: str,
    ref: str,
    dispatch_id: str,
    prompt: str,
    story_ref: str = "",
) -> list[str]:
    """Construct argv for the workflow_dispatch trigger via `gh workflow run`."""
    return [
        "gh",
        "workflow",
        "run",
        workflow,
        "--ref",
        ref,
        "-f",
        f"dispatch_id={dispatch_id}",
        "-f",
        f"story_ref={story_ref}",
        "-f",
        f"prompt={prompt}",
    ]


def build_run_list_argv(*, workflow: str, ref: str) -> list[str]:
    """Construct argv to list recent runs of the workflow on the dispatched ref."""
    return [
        "gh",
        "run",
        "list",
        "--workflow",
        workflow,
        "--branch",
        ref,
        "--json",
        "databaseId,displayTitle,status,conclusion,createdAt",
        "--limit",
        "30",
    ]


def build_run_view_argv(*, run_id: str) -> list[str]:
    """Construct argv to fetch one run's status and timing."""
    return [
        "gh",
        "run",
        "view",
        run_id,
        "--json",
        "databaseId,status,conclusion,createdAt,startedAt,updatedAt,url",
    ]


def build_run_download_argv(*, run_id: str, dest: str) -> list[str]:
    """Construct argv to download all artifacts of a run."""
    return ["gh", "run", "download", run_id, "--dir", dest]


def build_run_cancel_argv(*, run_id: str) -> list[str]:
    """Construct argv to cancel an in-flight run (local timeout hit)."""
    return ["gh", "run", "cancel", run_id]


# ── Subprocess helpers ────────────────────────────────────────────────


def _gh(
    argv: list[str],
    *,
    working_dir: Path,
    env: dict[str, str],
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(working_dir),
        timeout=timeout,
        env=env,
    )


def _current_ref(working_dir: Path) -> str:
    """Return the git branch of the workspace — the ref the workflow runs against."""
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(working_dir),
    )
    ref = proc.stdout.strip()
    return ref if proc.returncode == 0 and ref and ref != "HEAD" else "main"


def _discover_run_id(
    *,
    workflow: str,
    ref: str,
    dispatch_id: str,
    working_dir: Path,
    env: dict[str, str],
    deadline: float,
) -> str | None:
    """Poll `gh run list` until the run carrying our dispatch id appears."""
    discovery_deadline = min(deadline, time.monotonic() + _RUN_DISCOVERY_TIMEOUT_SECONDS)
    while time.monotonic() < discovery_deadline:
        proc = _gh(
            build_run_list_argv(workflow=workflow, ref=ref),
            working_dir=working_dir,
            env=env,
        )
        if proc.returncode == 0:
            try:
                runs = json.loads(proc.stdout or "[]")
            except json.JSONDecodeError:
                runs = []
            for run in runs:
                if dispatch_id in (run.get("displayTitle") or ""):
                    return str(run.get("databaseId"))
        time.sleep(_poll_seconds())
    return None


def _collect_artifact_text(dest: Path) -> tuple[str, dict[str, int]]:
    """Fold downloaded artifact files into one text blob plus a size index.

    Text-bearing files are concatenated with per-file headers so trace
    artifacts preserve which capture stream each section came from.
    """
    index: dict[str, int] = {}
    sections: list[str] = []
    budget = _MAX_ARTIFACT_TEXT_BYTES
    for path in sorted(dest.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(dest))
        size = path.stat().st_size
        index[rel] = size
        if path.suffix.lower() not in _ARTIFACT_TEXT_SUFFIXES or budget <= 0:
            continue
        try:
            text = path.read_text(errors="replace")[:budget]
        except OSError:
            continue
        budget -= len(text)
        sections.append(f"── artifact: {rel} ──\n{text}")
    return "\n\n".join(sections), index


def _failure(
    *,
    profile: ModelProfile,
    output: str,
    exit_code: int = 1,
    startup_failure: bool = False,
    raw: dict[str, Any] | None = None,
) -> AgentResult:
    return AgentResult(
        success=False,
        output=output,
        session_id=None,
        cost_usd=None,
        exit_code=exit_code,
        raw=raw or {},
        profile_name=profile.name,
        startup_failure=startup_failure,
    )


# ── gh-aw runner ──────────────────────────────────────────────────────


def _run_ghaw(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
    quiet: bool = False,
    is_pool: bool = False,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Dispatch the dev-phase agentic workflow and collect its result.

    session resume does not exist on this transport — each dispatch is a
    fresh Actions run, so session_id/is_pool are accepted for interface
    parity and ignored. The returned session_id is the Actions run id,
    which is a durable audit pointer rather than a resume handle.
    """
    del session_id, is_pool

    label = profile.name or f"ghaw/{profile.model}"
    workflow = workflow_file()
    if len(prompt) > _MAX_PROMPT_CHARS:
        return _failure(
            profile=profile,
            output=(
                f"PROMPT_TOO_LARGE: workflow_dispatch inputs cap the payload at 65535 "
                f"characters; prompt is {len(prompt)} chars (limit {_MAX_PROMPT_CHARS}). "
                "The gh-aw transport cannot carry this dev prompt."
            ),
            startup_failure=True,
        )

    env = build_workspace_env(working_dir, extra=secrets)
    ref = _current_ref(working_dir)
    dispatch_id = uuid.uuid4().hex[:12]
    deadline = time.monotonic() + profile.timeout_seconds
    timing: dict[str, Any] = {"ref": ref, "workflow": workflow, "dispatch_id": dispatch_id}

    if not quiet:
        _log(f"  Starting {label} (workflow={workflow}, ref={ref}, dispatch={dispatch_id})...")

    # 1. Dispatch (the workflow_dispatch event).
    t_dispatch = time.monotonic()
    timing["dispatched_at_unix"] = time.time()
    try:
        proc = _gh(
            build_argv(
                workflow=workflow,
                ref=ref,
                dispatch_id=dispatch_id,
                prompt=prompt,
            ),
            working_dir=working_dir,
            env=env,
        )
    except FileNotFoundError:
        return _failure(
            profile=profile,
            output="STARTUP_FAILURE: 'gh' not found in PATH",
            exit_code=-1,
            startup_failure=True,
        )
    if proc.returncode != 0:
        return _failure(
            profile=profile,
            output=f"DISPATCH_FAILED: gh workflow run exited {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}",
            exit_code=proc.returncode,
            startup_failure=True,
        )

    # 2. Correlate the dispatch to a concrete run id via the run-name marker.
    run_id = _discover_run_id(
        workflow=workflow,
        ref=ref,
        dispatch_id=dispatch_id,
        working_dir=working_dir,
        env=env,
        deadline=deadline,
    )
    if run_id is None:
        return _failure(
            profile=profile,
            output=(
                f"RUN_NOT_FOUND: dispatched {workflow} on {ref} (dispatch {dispatch_id}) "
                "but no matching run appeared before the discovery timeout"
            ),
            raw={"timing": timing},
        )
    timing["discovery_seconds"] = round(time.monotonic() - t_dispatch, 1)
    if not quiet:
        _log_verbose(f"  ... {label} run {run_id} discovered ({timing['discovery_seconds']}s)")

    # 3. Poll to completion within the profile's wall-clock budget.
    run_json: dict[str, Any] = {}
    while True:
        if time.monotonic() > deadline:
            _gh(build_run_cancel_argv(run_id=run_id), working_dir=working_dir, env=env)
            return _failure(
                profile=profile,
                output=(
                    f"TIMEOUT: run {run_id} still {run_json.get('status', 'unknown')!r} after "
                    f"{profile.timeout_seconds}s — cancel requested"
                ),
                exit_code=-1,
                raw={"run": run_json, "timing": timing},
            )
        proc = _gh(build_run_view_argv(run_id=run_id), working_dir=working_dir, env=env)
        if proc.returncode == 0:
            try:
                run_json = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                run_json = {}
            if run_json.get("status") == "completed":
                break
        time.sleep(_poll_seconds())
    timing["total_seconds"] = round(time.monotonic() - t_dispatch, 1)

    # 4. Collect artifacts — the capture-fidelity surface of this transport.
    artifact_text = ""
    artifact_index: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="ghaw-artifacts-") as tmp:
        proc = _gh(
            build_run_download_argv(run_id=run_id, dest=tmp),
            working_dir=working_dir,
            env=env,
            timeout=300.0,
        )
        if proc.returncode == 0:
            artifact_text, artifact_index = _collect_artifact_text(Path(tmp))
        else:
            artifact_text = (
                f"ARTIFACT_DOWNLOAD_FAILED: gh run download exited {proc.returncode}: "
                f"{proc.stderr.strip()}"
            )

    conclusion = run_json.get("conclusion") or "unknown"
    success = conclusion == "success"
    output = artifact_text or f"(no text artifacts) run {run_id} concluded {conclusion!r}"
    if not quiet:
        _log(
            f"  ... {label} done | {'OK' if success else 'FAIL'} | "
            f"run={run_id} conclusion={conclusion} ({timing['total_seconds']}s)"
        )

    return AgentResult(
        success=success,
        output=output,
        session_id=str(run_id),
        cost_usd=None,
        exit_code=0 if success else 1,
        raw={"run": run_json, "artifacts": artifact_index, "timing": timing},
        profile_name=profile.name,
        dev_handoff=_try_parse_handoff(output),
    )
