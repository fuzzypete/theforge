"""Coordinator gate execution, dirty-worktree detection, and auto-commit."""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

from . import coord_util as _cu
from .artifacts import ensure_parent_dir, resolve_handoff_path
from .config import ForgeConfig
from .task import TaskStory as TaskSpec  # noqa: F401
from .traces import write_trace


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
    handoff_path = resolve_handoff_path(workspace_path, config.validation.handoff_file)
    if handoff_path is None or not handoff_path.exists():
        return None, (f"handoff file not found: {workspace_path / config.validation.handoff_file}")

    try:
        with open(handoff_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return None, f"Failed to parse handoff YAML: {e}"
    except OSError as e:
        return None, f"Failed to read handoff file: {e}"

    decision = data.get(config.validation.gate_decision_key)
    if decision is None:
        try:
            handoff_label = str(handoff_path.relative_to(workspace_path))
        except ValueError:
            handoff_label = str(handoff_path)
        return None, (f"Key {config.validation.gate_decision_key!r} not found in {handoff_label}")

    return str(decision).upper(), None


def _write_gate_decision(config: ForgeConfig, workspace_path: Path, decision: str) -> None:
    """Merge gate_decision into handoff.yaml without overwriting other keys."""
    handoff_path = workspace_path / config.validation.handoff_file
    try:
        data: dict = {}
        if handoff_path.exists():
            with open(handoff_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            # Only reuse the file if it parsed as a mapping; otherwise start fresh.
            if isinstance(loaded, dict):
                data = loaded
        data[config.validation.gate_decision_key] = decision
        ensure_parent_dir(handoff_path)
        with open(handoff_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    except (OSError, yaml.YAMLError) as e:
        _cu._log(
            f"Warning: could not write gate_decision to {config.validation.handoff_file}: {e}"
        )


def _run_gate_full(
    config: ForgeConfig,
    workspace_path: Path,
    task: TaskSpec | None = None,
    iter_num: int | None = None,
) -> tuple[str | None, str | None, str]:
    """Run the gate command and read the decision. Returns (decision, error, output_tail)."""
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
            f"Gate timed out (gate_timeout={config.validation.gate_timeout}s)."
            " Consider increasing gate_timeout.",
            output_tail,
        )
    if output.startswith("ERROR:"):
        return None, f"Gate infrastructure error: {output[:300]}", output_tail

    # Gate decision comes from exit code. Write it into handoff.yaml (merging, not
    # overwriting) so downstream validation sees gate_decision alongside dev notes.
    decision = "PASS" if ok else "FAIL"
    if config.validation.handoff_file:
        _write_gate_decision(config, workspace_path, decision)
    if not ok:
        _cu._log(f"Gate command failed (exit non-zero): {output_tail}")
    return decision, None, output_tail


def _run_gate(
    config: ForgeConfig, workspace_path: Path, task: TaskSpec | None = None
) -> tuple[str | None, str | None, str]:
    """Run the gate command. Returns (decision, error, output_tail)."""
    return _run_gate_full(config, workspace_path, task)
