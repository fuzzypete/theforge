"""Coordinator gate execution, dirty-worktree detection, and auto-commit."""

from __future__ import annotations

import shlex
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.coordinator.state import GateDebugTelemetry
from theforge.task import TaskStory
from theforge.traces import write_trace

from . import util as _cu


def _parse_dirty_files(raw_output: str) -> list[str]:
    """Parse filenames from ``git status --porcelain`` output.

    Returns tracked modified/added/deleted/renamed filenames.
    Skips untracked (``??``) and ignored (``!!``) entries using the standard
    porcelain v1 two-character prefix only.
    For renames (``R`` status) returns the destination filename.
    """
    dirty: list[str] = []
    for line in raw_output.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        if xy in ("??", "!!"):
            continue
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        dirty.append(rest.strip())
    return dirty


def _auto_commit_side_effects(workspace_path: Path, files: list[str]) -> bool:
    """Stage and commit out-of-scope files as fmt side-effects.

    Uses explicit filenames (not ``-A``) so only the intended files are staged.
    Returns True if the commit succeeded, False on any git error (fail-safe).
    """
    try:
        quoted = " ".join(shlex.quote(f) for f in files)
        add_ok, add_out = _cu._run_shell(f"git add -- {quoted}", workspace_path)
        if not add_ok:
            _cu._log(f"Auto-commit: git add failed: {add_out}")
            return False
        commit_ok, commit_out = _cu._run_shell(
            'git commit -m "chore: auto-commit fmt side-effects"', workspace_path
        )
        if not commit_ok:
            _cu._log(f"Auto-commit: git commit failed: {commit_out}")
            return False
        n = len(files)
        _cu._log(f"Auto-committed {n} out-of-scope fmt side-effects: {', '.join(files)}")
        return True
    except Exception as e:  # noqa: BLE001
        _cu._log(f"Auto-commit: unexpected error: {e}")
        return False


def _is_gate_skip(gate_override: str | None) -> bool:
    """Return True if the gate_override value means 'skip the gate entirely'."""
    return isinstance(gate_override, str) and gate_override.lower() == "none"


def _run_gate_full(
    config: ForgeConfig,
    workspace_path: Path,
    task: TaskStory | None = None,
    iter_num: int | None = None,
) -> tuple[str | None, str | None, str, str]:
    """Run the gate command and determine pass/fail from exit code.

    Returns (decision, error, output_tail, resolved_gate_cmd).
    decision is "PASS" or "FAIL"; error is set only on infrastructure failure.
    """
    has_override = (
        task is not None and task.gate_override and not _is_gate_skip(task.gate_override)
    )
    if has_override:
        gate_cmd = task.gate_override  # type: ignore[union-attr]
    else:
        gate_cmd = config.validation.gate_command
        if task is not None:
            pytest_target = task.pytest_target or "tests/"
            gate_cmd = gate_cmd.replace("{pytest_target}", pytest_target)
            gate_cmd = gate_cmd.replace("{slug}", task.slug)

    _cu._log_verbose(f"Running gate: {gate_cmd}")
    gate_timeout = config.validation.gate_timeout or 600
    ok, output = _cu._run_shell(
        gate_cmd,
        workspace_path,
        timeout=gate_timeout,
    )

    if iter_num is not None:
        write_trace(
            workspace_path / ".forge/traces" / f"{iter_num}-gate.txt",
            output,
        )

    tail_chars = config.validation.gate_output_tail_chars
    output_tail = output[-tail_chars:]

    if output.startswith("TIMEOUT"):
        return (
            None,
            f"Gate timed out after {gate_timeout}s",
            output_tail,
            gate_cmd,
        )
    if output.startswith("ERROR:"):
        return None, f"Gate infrastructure error: {output[:300]}", output_tail, gate_cmd

    decision = "PASS" if ok else "FAIL"
    if decision == "FAIL":
        _cu._log(f"Gate command failed (exit non-zero): {output_tail}")
    return decision, None, output_tail, gate_cmd


def _run_gate_debug_command(
    config: ForgeConfig,
    workspace_path: Path,
    *,
    iter_num: int,
) -> GateDebugTelemetry | None:
    """Run the configured gate debug command after a gate timeout."""
    debug_cmd = config.validation.gate_debug_command
    if not debug_cmd:
        return None

    gate_timeout = config.validation.gate_timeout or 600
    debug_timeout = config.validation.gate_debug_timeout or gate_timeout
    _cu._log(f"  Running gate debug command after timeout: {debug_cmd}")
    ok, output, exit_code, _timed_out = _cu._run_shell_detailed(
        debug_cmd,
        workspace_path,
        timeout=debug_timeout,
    )
    if output.startswith("TIMEOUT") and exit_code is None:
        _cu._log(f"  Gate debug command timed out after {debug_timeout}s")
    elif not ok:
        _cu._log(f"  Gate debug command exited non-zero: {exit_code}")

    tail_chars = config.validation.gate_output_tail_chars
    output_tail = output[-tail_chars:]
    write_trace(
        workspace_path / ".forge/traces" / f"{iter_num}-gate-debug.txt",
        output,
    )
    return GateDebugTelemetry(
        iteration=iter_num,
        command=debug_cmd,
        ran=True,
        timeout_s=debug_timeout,
        exit_code=exit_code,
        output_tail=output_tail,
        output_truncated=len(output) > len(output_tail),
    )


def _run_gate(
    config: ForgeConfig, workspace_path: Path, task: TaskStory | None = None
) -> tuple[str | None, str | None, str]:
    """Run the gate command. Returns (decision, error, output_tail)."""
    decision, error, output_tail, _resolved_cmd = _run_gate_full(config, workspace_path, task)
    return decision, error, output_tail
