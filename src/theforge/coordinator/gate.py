"""Coordinator gate execution, dirty-worktree detection, and auto-commit."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.coordinator.state import GateDebugTelemetry, GateLabel
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


def run_gate_full(
    config: ForgeConfig,
    workspace_path: Path,
    task: TaskStory | None = None,
    iter_num: int | None = None,
    *,
    output_digest: list[str] | None = None,
    full_output: list[str] | None = None,
    label: GateLabel | None = None,
) -> tuple[str | None, str | None, str, str, int | None]:
    """Run the gate command and determine pass/fail from exit code.

    Returns (decision, error, output_tail, resolved_gate_cmd, exit_code).
    decision is "PASS" or "FAIL"; error is set only on infrastructure failure.

    ``output_digest``, when given, receives a single SHA-256 of the **full**
    output. Callers comparing one gate run against the next must fingerprint the
    whole output, not ``output_tail``: the tail is the last
    ``gate_output_tail_chars`` characters, so a gate whose output ends in a
    constant footer (a coverage table, a fixed summary banner) hashes identically
    on every run while the failure detail changes above the window (#1981).

    It is an out-parameter rather than a sixth return value because the 5-tuple is
    unpacked at every call site including ~30 mocked tests; widening it — or
    returning a record — would churn all of them to carry one scalar that only
    one caller reads. A caller-supplied list keeps it explicit and per-call, so
    parallel story workers cannot share it.

    ``full_output``, when given, receives the **full** raw output as a single
    element. It exists for callers whose gate runs in a workspace that will not
    survive the call — the baseline gate runs in a temporary worktree, so the
    ``iter_num`` trace above (written *into* that workspace) is destroyed with
    it. Such a caller needs the bytes in hand to persist them somewhere durable;
    only the caller knows where that is. Same out-parameter shape, and for the
    same reason, as ``output_digest``. It is populated on every path the gate
    command actually ran, including timeout and infrastructure error, since
    those are exactly the outcomes whose evidence is hardest to recover.

    ``label``, when given, names the gate's purpose and target in the "Running
    gate" log line so semantically different gates that resolve to the same
    command (baseline vs. per-story reuse vs. validation) are distinguishable
    in the sprint log (#2014). It is display-only: it never reaches the shell
    command, the timeout, or the decision.
    """
    has_override = (
        task is not None and task.gate_override and not _is_gate_skip(task.gate_override)
    )
    if has_override:
        gate_cmd = task.gate_override  # type: ignore[union-attr]
    else:
        gate_cmd = config.validation.gate_command
        default_target = config.validation.default_test_target or "."
        test_target = (task.test_target if task is not None else None) or default_target
        slug = task.slug if task is not None else "baseline"
        gate_cmd = gate_cmd.replace("{test_target}", test_target)
        gate_cmd = gate_cmd.replace("{slug}", slug)

    gate_identity = label.describe() if label is not None else "gate"
    _cu._log_verbose(f"Running {gate_identity}: {gate_cmd}")
    gate_timeout = config.validation.gate_timeout or 600
    ok, output, exit_code, timed_out = _cu._run_shell_detailed(
        gate_cmd,
        workspace_path,
        timeout=gate_timeout,
        expected_python=config.workspace.python_interpreter,
    )

    if iter_num is not None:
        write_trace(
            workspace_path / ".forge/traces" / f"{iter_num}-gate.txt",
            output,
        )

    if output_digest is not None:
        output_digest.append(hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest())

    if full_output is not None:
        full_output.append(output)

    tail_chars = config.validation.gate_output_tail_chars
    output_tail = output[-tail_chars:]

    if timed_out or output.startswith("TIMEOUT"):
        return (
            None,
            f"Gate timed out after {gate_timeout}s",
            output_tail,
            gate_cmd,
            exit_code,
        )
    if output.startswith("ERROR:"):
        return None, f"Gate infrastructure error: {output[:300]}", output_tail, gate_cmd, exit_code

    decision = "PASS" if ok else "FAIL"
    if decision == "FAIL":
        _cu._log(f"Gate command failed (exit non-zero): {output_tail}")
    return decision, None, output_tail, gate_cmd, exit_code


def format_gate_failure_summary(
    header: str,
    *,
    exit_code: int | None,
    output_tail: str,
    tail_chars: int,
    trace_path: str | None = None,
) -> str:
    """Attach gate triage evidence to a terminal gate-failure outcome string.

    ``header`` is the verdict phrase (e.g. "Gate returned FAIL after 3
    attempts"). The gate exit code and output tail are appended verbatim so a
    reader of ``forge status`` detail or the escalation issue comment can tell a
    compile error from a flake from an infrastructure failure without opening
    the run log — the evidence travels with the verdict rather than sitting in a
    file the outcome never references. When a full-output trace was written its
    path is named too (it survives worktree cleanup only for preserved ESCALATE
    worktrees, so the tail is always carried inline as well). Nothing here
    interprets the output; it is copied as-is, so the summary is independent of
    the gate command, language, and toolchain.
    """
    header_line = header if exit_code is None else f"{header} (gate exit code {exit_code})"
    parts = [header_line]
    tail = (output_tail or "").strip()
    if tail:
        parts.append(f"Gate output tail (last {tail_chars} chars):\n{tail}")
    else:
        parts.append("Gate captured no output.")
    if trace_path:
        parts.append(f"Full gate output trace: {trace_path}")
    return "\n".join(parts)


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
        expected_python=config.workspace.python_interpreter,
    )
    if output.startswith("TIMEOUT") and exit_code is None:
        _cu._log(f"  Gate debug command timed out after {debug_timeout}s")
    elif not ok:
        _cu._log(f"  Gate debug command exited non-zero: {exit_code}")

    tail_chars = config.validation.gate_output_tail_chars
    output_tail = output[-tail_chars:]
    # One expression names the trace file and the field that points at it, so the
    # entry can never quote a path it did not write (#1986).
    trace_rel = f".forge/traces/{iter_num}-gate-debug.txt"
    write_trace(workspace_path / trace_rel, output)
    return GateDebugTelemetry(
        trace_index=iter_num,
        trace_path=trace_rel,
        command=debug_cmd,
        ran=True,
        timeout_s=debug_timeout,
        exit_code=exit_code,
        output_tail=output_tail,
        output_truncated=len(output) > len(output_tail),
    )


def _run_gate(
    config: ForgeConfig,
    workspace_path: Path,
    task: TaskStory | None = None,
    *,
    label: GateLabel | None = None,
) -> tuple[str | None, str | None, str]:
    """Run the gate command. Returns (decision, error, output_tail)."""
    decision, error, output_tail, _resolved_cmd, _exit_code = run_gate_full(
        config, workspace_path, task, label=label
    )
    return decision, error, output_tail
