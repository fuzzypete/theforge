"""Coordinator gate execution, dirty-worktree detection, and auto-commit."""

from __future__ import annotations

import hashlib
import shlex
import shutil
from pathlib import Path

from theforge.config import PYTHON_VERSION_PLACEHOLDER, ForgeConfig
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


def _digest(output: str) -> str:
    """SHA-256 of gate output, used to tell one gate run from the next."""
    return hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()


def _missing_matrix_interpreters(versions: tuple[str, ...]) -> list[str]:
    """Return the ``pythonX.Y`` executables the declared matrix needs but PATH lacks."""
    return [f"python{v}" for v in versions if shutil.which(f"python{v}") is None]


def _run_gate_leg(
    config: ForgeConfig,
    workspace_path: Path,
    gate_cmd: str,
    *,
    trace_name: str | None,
    gate_timeout: int,
) -> tuple[str | None, str | None, str, int | None]:
    """Run one gate invocation. Returns (decision, error, full_output, exit_code).

    ``decision`` is "PASS"/"FAIL" from the exit code, or None when ``error`` is
    set — a timeout or infrastructure failure is not a verdict on the commit.
    ``expected_python`` stays the project pin regardless of which matrix leg this
    is: it only governs whether the worktree's ``.venv`` goes on PATH, and a leg
    selects its own environment through the gate command itself.
    """
    _cu._log_verbose(f"Running gate: {gate_cmd}")
    ok, output, exit_code, timed_out = _cu._run_shell_detailed(
        gate_cmd,
        workspace_path,
        timeout=gate_timeout,
        expected_python=config.workspace.python_interpreter,
    )

    if trace_name is not None:
        write_trace(workspace_path / ".forge/traces" / trace_name, output)

    if timed_out or output.startswith("TIMEOUT"):
        return None, f"Gate timed out after {gate_timeout}s", output, exit_code
    if output.startswith("ERROR:"):
        return None, f"Gate infrastructure error: {output[:300]}", output, exit_code
    return ("PASS" if ok else "FAIL"), None, output, exit_code


def run_gate_full(
    config: ForgeConfig,
    workspace_path: Path,
    task: TaskStory | None = None,
    iter_num: int | None = None,
    *,
    output_digest: list[str] | None = None,
) -> tuple[str | None, str | None, str, str, int | None]:
    """Run the gate command and determine pass/fail from exit code.

    Returns (decision, error, output_tail, resolved_gate_cmd, exit_code).
    decision is "PASS" or "FAIL"; error is set only on infrastructure failure.

    When ``validation.python_versions`` is set the gate is a matrix: the command
    runs once per declared interpreter with ``{python_version}`` substituted, and
    PASS requires every leg to pass. This exists so the gate that clears a story
    covers the same environment surface as the project's required merge checks —
    a single-interpreter gate greenlit changes the CI matrix then rejected, so
    "complete" was reported for commits that could not land (#1945). A missing
    interpreter is an infrastructure error, not a FAIL: the host, not the commit,
    is what is wrong, and routing it back to dev would ask the agent to fix
    something outside the worktree.

    Legs run in declared order and stop at the first non-PASS. The commit has to
    change either way, and continuing would spend another full gate_timeout per
    remaining leg for evidence the dev does not need yet.

    A story-level ``gate_override`` runs single-leg and unsubstituted. It is an
    explicit operator escape hatch that already bypasses the configured command
    entirely; the matrix is a property of that command, not of the override.

    ``output_digest``, when given, receives a single SHA-256 of the **full**
    output (all legs run, concatenated). Callers comparing one gate run against
    the next must fingerprint the whole output, not ``output_tail``: the tail is
    the last ``gate_output_tail_chars`` characters, so a gate whose output ends
    in a constant footer (a coverage table, a fixed summary banner) hashes
    identically on every run while the failure detail changes above the window
    (#1981).

    It is an out-parameter rather than a sixth return value because the 5-tuple is
    unpacked at every call site including ~30 mocked tests; widening it — or
    returning a record — would churn all of them to carry one scalar that only
    one caller reads. A caller-supplied list keeps it explicit and per-call, so
    parallel story workers cannot share it.
    """
    has_override = (
        task is not None and task.gate_override and not _is_gate_skip(task.gate_override)
    )
    if has_override:
        gate_cmd = task.gate_override  # type: ignore[union-attr]
        versions: tuple[str, ...] = ()
    else:
        gate_cmd = config.validation.gate_command
        default_target = config.validation.default_test_target or "."
        test_target = (task.test_target if task is not None else None) or default_target
        slug = task.slug if task is not None else "baseline"
        gate_cmd = gate_cmd.replace("{test_target}", test_target)
        gate_cmd = gate_cmd.replace("{slug}", slug)
        versions = config.validation.python_versions

    gate_timeout = config.validation.gate_timeout or 600
    tail_chars = config.validation.gate_output_tail_chars

    if not versions:
        decision, error, output, exit_code = _run_gate_leg(
            config,
            workspace_path,
            gate_cmd,
            trace_name=(f"{iter_num}-gate.txt" if iter_num is not None else None),
            gate_timeout=gate_timeout,
        )
        if output_digest is not None:
            output_digest.append(_digest(output))
        output_tail = output[-tail_chars:]
        if decision == "FAIL":
            _cu._log(f"Gate command failed (exit non-zero): {output_tail}")
        return decision, error, output_tail, gate_cmd, exit_code

    missing = _missing_matrix_interpreters(versions)
    if missing:
        declared = ", ".join(versions)
        error = (
            f"Gate matrix cannot run: {', '.join(missing)} not found on PATH. "
            f"validation.python_versions declares {declared}; install the missing "
            "interpreter(s) or change the declared matrix."
        )
        _cu._log(error)
        if output_digest is not None:
            output_digest.append(_digest(error))
        return None, error, error[-tail_chars:], gate_cmd, None

    _cu._log(f"Gate matrix: {len(versions)} interpreter(s) — {', '.join(versions)}")
    outputs: list[str] = []
    leg_cmd = gate_cmd
    exit_code: int | None = None
    for version in versions:
        leg_cmd = gate_cmd.replace(PYTHON_VERSION_PLACEHOLDER, version)
        _cu._log(f"  Gate leg python{version}: {leg_cmd}")
        decision, error, output, exit_code = _run_gate_leg(
            config,
            workspace_path,
            leg_cmd,
            trace_name=(f"{iter_num}-gate-py{version}.txt" if iter_num is not None else None),
            gate_timeout=gate_timeout,
        )
        outputs.append(f"=== gate matrix leg: python{version} ===\n{output}")
        if decision == "PASS":
            continue

        # Failing leg's output is last, so the tail window shows its detail.
        combined = "\n".join(outputs)
        if output_digest is not None:
            output_digest.append(_digest(combined))
        output_tail = combined[-tail_chars:]
        if error is not None:
            return None, f"[python{version}] {error}", output_tail, leg_cmd, exit_code
        _cu._log(f"Gate command failed on python{version} (exit non-zero): {output_tail}")
        return "FAIL", None, output_tail, leg_cmd, exit_code

    combined = "\n".join(outputs)
    if output_digest is not None:
        output_digest.append(_digest(combined))
    return "PASS", None, combined[-tail_chars:], leg_cmd, exit_code


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
    decision, error, output_tail, _resolved_cmd, _exit_code = run_gate_full(
        config, workspace_path, task
    )
    return decision, error, output_tail
