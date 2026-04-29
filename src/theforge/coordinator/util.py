"""Coordinator utilities: logging, run-id generation, and shell execution.

Extracted from coord_state.py so that coord_state.py can remain stdlib-only
(dataclasses/enums only, no project imports at runtime).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from theforge.coordinator.log_tee import get_worker_slug
from theforge.log_level import LogLevel
from theforge.log_util import _log_line
from theforge.workspace_env import build_workspace_env

# Stable reference captured at import time so test patches that replace the
# subprocess module attribute (e.g. patch("validate_phase.subprocess.run"))
# do not accidentally intercept worktree eval subprocess calls.
_subprocess_run = subprocess.run

# Absolute path to the subprocess eval entry point — invoked by path so the
# coordinator's own version runs regardless of what the installed package provides.
_SUBPROCESS_EVAL = Path(__file__).parent / "_subprocess_eval.py"

MEDIUM_HEADROOM_FACTOR = 1.5
LARGE_HEADROOM_FACTOR = 2.5

# ── Log level ─────────────────────────────────────────────────────────

_LOG_LEVEL: LogLevel = LogLevel.PROGRESS


def set_log_level(level: LogLevel) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level


# ── Logging ──────────────────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    """Format duration as '2h 14m 3s', '14m 3s', or '47s'."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _log(msg: str) -> None:
    """Print coordinator status to stderr (always shown)."""
    slug = get_worker_slug()
    prefix = f"[{slug}] " if slug else ""
    _log_line("[forge]", f"{prefix}{msg}")


def _log_verbose(msg: str) -> None:
    """Print coordinator detail to stderr (verbose mode only)."""
    if _LOG_LEVEL >= LogLevel.VERBOSE:
        slug = get_worker_slug()
        prefix = f"[{slug}] " if slug else ""
        _log_line("[forge]", f"{prefix}{msg}")


def _fmt_cost(cost: float | None) -> str:
    """Format a cost value as '$1.23', or 'unknown' when cost is None."""
    return f"${cost:.2f}" if cost is not None else "unknown"


def _log_phase(phase: object, detail: str = "") -> None:
    suffix = f"   {detail}" if detail else ""
    _log(f"▸ {phase.name}{suffix}")  # type: ignore[attr-defined]


# ── Run ID ───────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Return a short random hex run ID (12 chars)."""
    return secrets.token_hex(6)


# ── Shell helper ─────────────────────────────────────────────────────


def resolve_timeout(
    base: int,
    medium: int | None,
    large: int | None,
    complexity: str | None,
    complexity_score: int | None = None,
) -> int:
    """Return the appropriate timeout for the given preflight complexity.

    Converted routing sites prefer the numeric score when present so stories in
    the same legacy band can still diverge. Current policy: 8-10 uses the large
    override, 6-7 uses the medium override, and lower scores fall back to base.
    When no score is available, legacy band-based behavior is preserved.
    """
    return resolve_timeout_with_active(base, medium, large, complexity, complexity_score)[0]


def resolve_timeout_with_active(
    base: int,
    medium: int | None,
    large: int | None,
    complexity: str | None,
    complexity_score: int | None = None,
) -> tuple[int, bool]:
    """Return ``(timeout, override_active)`` for the given complexity inputs.

    Score-native routing preserves tier isolation: large-score stories use the
    large tier and medium-score stories use the medium tier. When an explicit
    tier override is absent, the timeout is derived from the base timeout using
    the corresponding headroom factor instead of silently falling back to base.
    Large stories must not cascade into the medium tier when the large tier is
    unset.
    """

    def _derived_timeout(factor: float) -> int:
        return round(base * factor)

    if complexity_score is not None:
        if complexity_score >= 8:
            if large is not None:
                return large, True
            return _derived_timeout(LARGE_HEADROOM_FACTOR), True
        if complexity_score >= 6:
            if medium is not None:
                return medium, True
            return _derived_timeout(MEDIUM_HEADROOM_FACTOR), True
        return base, False
    if complexity == "large":
        if large is not None:
            return large, True
        return _derived_timeout(LARGE_HEADROOM_FACTOR), True
    if complexity == "medium":
        if medium is not None:
            return medium, True
        return _derived_timeout(MEDIUM_HEADROOM_FACTOR), True
    return base, False


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Best-effort kill for a spawned shell process group."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except OSError:
        pass
    try:
        proc.terminate()
    except OSError:
        pass


def _run_shell_detailed(
    cmd: str, cwd: Path, timeout: int = 120, env: dict[str, str] | None = None
) -> tuple[bool, str, int | None, bool]:
    """Run a shell command. Returns (success, combined output, exit_code, timed_out).

    On abnormal exit, kills the entire process group so child processes (e.g.
    pytest-xdist workers) don't outlive the shell and consume unbounded memory.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
            env=env if env is not None else build_workspace_env(cwd),
            start_new_session=True,
        )
    except Exception as e:
        return False, f"ERROR: {e}", None, False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        output = (stdout + stderr).strip()
        return proc.returncode == 0, output, proc.returncode, False
    except subprocess.TimeoutExpired as te:
        # Kill the whole process group before draining pipes so grandchildren
        # such as pytest-xdist workers cannot keep writing indefinitely after
        # the shell itself has timed out.
        _kill_process_group(proc)
        proc.wait()
        partial_out = ""
        try:
            chunks: list[str] = []
            for raw in (te.stdout, te.stderr):
                if raw:
                    chunks.append(raw if isinstance(raw, str) else raw.decode(errors="replace"))
            if proc.stdout:
                chunks.append(proc.stdout.read())
            if proc.stderr:
                chunks.append(proc.stderr.read())
            partial_out = "".join(chunks).strip()
        except Exception:
            pass
        header = f"TIMEOUT after {timeout}s: {cmd}"
        if partial_out:
            return False, f"{header}\n{partial_out}", None, True
        return False, header, None, True
    except BaseException:
        _kill_process_group(proc)
        proc.wait()
        raise
    finally:
        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


def _run_shell(
    cmd: str, cwd: Path, timeout: int = 120, env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Run a shell command. Returns (success, combined output)."""
    ok, output, _exit_code, _timed_out = _run_shell_detailed(
        cmd,
        cwd,
        timeout=timeout,
        env=env,
    )
    return ok, output


# ── Worktree project-code evaluation ────────────────────────────────────


def _run_worktree_eval(
    workspace_path: Path,
    command: str,
    payload: dict,
    timeout: int = 120,
) -> dict:
    """Run a project-code evaluation in the worktree's Python environment.

    Prepends workspace_path/src to PYTHONPATH so the subprocess imports
    theforge.* modules from the worktree rather than the coordinator's copies.
    This is the isolation boundary for self-hosting: project code is never
    imported into the coordinator's process; it runs only in this subprocess.

    Returns the parsed JSON result dict. Raises RuntimeError on subprocess
    failure.
    """
    env = os.environ.copy()
    worktree_src = str((workspace_path / "src").resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = worktree_src + (os.pathsep + existing if existing else "")

    result = _subprocess_run(
        [sys.executable, str(_SUBPROCESS_EVAL), command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(workspace_path),
        timeout=timeout,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"Worktree eval {command!r} failed (exit {result.returncode}): "
            f"{stderr or '(no stderr)'}"
        )

    return json.loads(result.stdout)
