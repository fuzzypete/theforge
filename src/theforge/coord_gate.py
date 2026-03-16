"""Coordinator gate execution, dirty-worktree detection, and auto-commit."""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from . import coord_util as _cu
from .config import ForgeConfig
from .task import TaskSpec


def _parse_dirty_files(raw_output: str) -> list[str]:
    """Parse filenames from ``git status --porcelain`` output.

    Returns tracked modified/added/deleted/renamed filenames.
    Skips untracked (``??``) and ignored (``!!``) entries.
    For renames (``R`` status) returns the destination filename.
    """
    dirty: list[str] = []
    for line in raw_output.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        if xy in ("??", "!!", " ?", " !"):
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


def _read_gate_decision(
    config: ForgeConfig, workspace_path: Path
) -> tuple[str | None, str | None]:
    """Read gate decision from handoff.yaml. Returns (decision, error)."""
    handoff_path = workspace_path / config.validation.handoff_file
    if not handoff_path.exists():
        return None, f"handoff file not found: {handoff_path}"

    try:
        with open(handoff_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return None, f"Failed to parse handoff YAML: {e}"
    except OSError as e:
        return None, f"Failed to read handoff file: {e}"

    decision = data.get(config.validation.gate_decision_key)
    if decision is None:
        return None, (
            f"Key {config.validation.gate_decision_key!r} not found in "
            f"{config.validation.handoff_file}"
        )

    return str(decision).upper(), None


def _run_gate_full(
    config: ForgeConfig, workspace_path: Path, task: TaskSpec | None = None
) -> tuple[str | None, str | None, str]:
    """Run the gate command and read the decision. Returns (decision, error, output_tail)."""
    has_override = (
        task is not None and task.gate_override and not _is_gate_skip(task.gate_override)
    )
    if has_override:
        gate_cmd = task.gate_override  # type: ignore[union-attr]
        use_exit_code = True
    else:
        if config.validation.handoff_file:
            stale_handoff = workspace_path / config.validation.handoff_file
            if stale_handoff.exists():
                try:
                    stale_handoff.unlink()
                except OSError as e:
                    return None, f"Cannot remove stale handoff file: {e}", ""
        gate_cmd = config.validation.gate_command
        if task is not None:
            pytest_target = task.pytest_target or "tests/"
            gate_cmd = gate_cmd.replace("{pytest_target}", pytest_target)
            gate_cmd = gate_cmd.replace("{slug}", task.slug)
        use_exit_code = not config.validation.handoff_file

    _cu._log_verbose(f"Running gate: {gate_cmd}")
    gate_timeout = config.validation.gate_timeout or 600
    ok, output = _cu._run_shell(
        gate_cmd,
        workspace_path,
        timeout=gate_timeout,
    )

    tail_chars = config.validation.gate_output_tail_chars
    output_tail = output[-tail_chars:]

    if use_exit_code:
        if ok:
            return "PASS", None, output_tail
        if output.startswith("TIMEOUT"):
            return (
                None,
                f"Gate timed out (gate_timeout={config.validation.gate_timeout}s)."
                " Consider increasing gate_timeout.",
                output_tail,
            )
        if output.startswith("ERROR:"):
            return None, f"Gate infrastructure error: {output[:300]}", output_tail
        _cu._log(f"Gate command failed (exit non-zero): {output_tail}")
        return "FAIL", None, output_tail

    if not ok:
        _cu._log_verbose(f"Gate command failed: {output[:200]}")
        decision, err = _read_gate_decision(config, workspace_path)
        if decision:
            return decision, None, output_tail
        return None, f"Gate command failed and no handoff produced: {output[:500]}", output_tail

    decision, err = _read_gate_decision(config, workspace_path)
    return decision, err, output_tail


def _run_gate(
    config: ForgeConfig, workspace_path: Path, task: TaskSpec | None = None
) -> tuple[str | None, str | None, str]:
    """Run the gate command. Returns (decision, error, output_tail)."""
    return _run_gate_full(config, workspace_path, task)
