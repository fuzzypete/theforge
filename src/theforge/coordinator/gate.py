"""Coordinator gate execution, dirty-worktree detection, and auto-commit."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.coordinator.state import GateDebugTelemetry, GateLabel, GateRunFacts
from theforge.process_group import ProcessTeardown
from theforge.task import TaskStory
from theforge.traces import write_trace
from theforge.validation_profiles import (
    PHASE_MERGE,
    SelectedValidation,
    override_selection,
    select_validation,
)

from . import util as _cu


def _parse_dirty_files(raw_output: str) -> list[str]:
    """Parse filenames from ``git status --porcelain`` output.

    Returns tracked modified/added/deleted/renamed filenames.
    Skips untracked (``??``) and ignored (``!!``) entries using the standard
    porcelain v1 two-character prefix only.
    For renames (``R`` status) returns the destination filename.

    The XY prefix is normally two columns wide, but every caller reads git
    through ``_run_shell``, which strips the combined output — so a first line
    whose index column is blank (`` M path``, the shape of an unstaged
    modification) arrives one character short. Slicing at a fixed offset then
    ate the first character of the path and the caller staged a pathspec that
    does not exist. Detect the short prefix instead of assuming the width.
    """
    dirty: list[str] = []
    for line in raw_output.splitlines():
        if len(line) < 4:
            continue
        if line[2] == " ":
            xy, rest = line[:2], line[3:]
        elif line[1] == " ":
            xy, rest = " " + line[0], line[2:]
        else:
            continue
        if xy in ("??", "!!"):
            continue
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        dirty.append(rest.strip())
    return dirty


def _snapshot_gate_worktree_state(workspace_path: Path) -> dict[str, object]:
    """Capture untracked/ignored paths visible at gate start for auditability.

    A gate verdict is a claim about the checked-out commit. When that verdict
    depends on worktree residue rather than tracked content, the record needs to
    say so explicitly. This capture is best-effort and never blocks the gate:
    failure to inspect the worktree is itself recorded rather than converted
    into a synthetic gate failure.
    """
    ok, output, _exit_code, _timed_out = _cu._run_shell_detailed(
        "git status --porcelain=v1 --ignored=matching --untracked-files=all",
        workspace_path,
    )
    state: dict[str, object] = {"untracked": [], "ignored": []}
    if not ok:
        state["capture_error"] = output.strip() or "git status failed"
        return state

    untracked: list[str] = []
    ignored: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        if line[2] == " ":
            xy, rest = line[:2], line[3:]
        elif line[1] == " ":
            xy, rest = " " + line[0], line[2:]
        else:
            continue
        path = rest.split(" -> ", 1)[1] if " -> " in rest else rest
        path = path.strip()
        if path.startswith(".forge/"):
            continue
        if xy == "??":
            untracked.append(path)
        elif xy == "!!":
            ignored.append(path)

    state["untracked"] = untracked
    state["ignored"] = ignored
    return state


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
    process_teardowns: list[ProcessTeardown] | None = None,
    label: GateLabel | None = None,
    selection_out: list[SelectedValidation] | None = None,
    worktree_state_out: list[dict[str, object]] | None = None,
    facts_out: list[GateRunFacts] | None = None,
    ignore_gate_override: bool = False,
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

    ``process_teardowns``, when given, receives a `ProcessTeardown` if the gate
    command left processes running that teardown had to kill — a test runner's
    workers outliving the run that started them (#2309). Same out-parameter shape
    as ``output_digest``, and for the same reason: it is a fact about the run's
    aftermath rather than part of the gate's verdict, and a caller that does not
    record it should not have to unpack it. Without it the only trace of a leak
    the gate caused is a log line.

    ``label``, when given, names the gate's purpose and target in the "Running
    gate" log line so semantically different gates that resolve to the same
    command (baseline vs. per-story reuse vs. validation) are distinguishable
    in the sprint log (#2014). It is display-only: it never reaches the shell
    command, the timeout, or the decision.

    ``selection_out``, when given, receives the `SelectedValidation` this run
    executed — which profile it was and what authority its result carries. Same
    out-parameter shape as ``output_digest``, and for the same reason: the
    5-tuple is unpacked at ~30 call sites including mocked tests, and a caller
    that does not persist provenance should not have to unpack it. A caller that
    records a verdict needs it, because a verdict is only a verdict if the
    profile behind it carries merge authority (#2358).

    ``facts_out``, when given, receives a `GateRunFacts` recording whether the
    run was killed at its budget and what that resolved budget was. The verdict
    tuple cannot answer either question — a timeout arrives as an error *string*
    — so a caller that must act differently on "did not finish" than on "failed"
    would otherwise have to match that text and re-derive the budget from config
    (#2796). Same out-parameter shape as ``selection_out``; only populated once
    the gate command actually ran.

    ``worktree_state_out``, when given, receives a dict naming the untracked
    and ignored paths visible in the worktree immediately before the gate
    command runs. This is audit provenance, not verdict logic: a gate that
    passes or fails because of local residue still keeps its exit-code result,
    but the record now says what residue was present when that result was
    produced.

    ``ignore_gate_override`` runs the configured profile even when the story
    carries a ``gate_override``. It exists for one caller: VALIDATE widening a
    passing *advisory* override to the merge-authority profile, so a story
    cannot reach REVIEW on a result that carries no merge authority. It is not a
    way to disable story overrides generally — the override still runs first,
    and its (advisory) result is still recorded.
    """
    has_override = (
        not ignore_gate_override
        and task is not None
        and task.gate_override
        and not _is_gate_skip(task.gate_override)
    )
    if has_override:
        gate_cmd = task.gate_override  # type: ignore[union-attr]
        # An undeclared command cannot inherit a declared profile's standing.
        # On the legacy path it keeps the authority it has always had; once a
        # project declares profiles, the override runs but its result is
        # advisory — merge trust belongs to the declared profile alone.
        selection = override_selection(gate_cmd, declared=bool(config.validation.profiles))
    else:
        selection = select_validation(config.validation, phase=PHASE_MERGE, task=task)
        gate_cmd = selection.command
    if selection_out is not None:
        selection_out.append(selection)

    gate_identity = label.describe() if label is not None else "gate"
    _cu._log_verbose(f"Running {gate_identity}: {gate_cmd}")
    if config.validation.profiles:
        # Only once a project declares profiles is there a selection to report:
        # which one ran and what its result will be worth. Kept off the "Running
        # gate" line, whose shape names the gate's purpose and target (#2014).
        _cu._log_verbose(f"  validation profile: {selection.describe()}")
    gate_timeout = config.validation.gate_timeout or 600
    gate_worktree_state = _snapshot_gate_worktree_state(workspace_path)
    if worktree_state_out is not None:
        worktree_state_out.append(gate_worktree_state)
    # Passed only when the caller asked for it: an out-parameter nobody supplied
    # is a no-op, and not sending it keeps this call compatible with the stubs
    # that stand in for the shell across the suite.
    _teardown_kwargs = {} if process_teardowns is None else {"teardown_out": process_teardowns}
    ok, output, exit_code, timed_out = _cu._run_shell_detailed(
        gate_cmd,
        workspace_path,
        timeout=gate_timeout,
        expected_python=config.workspace.python_interpreter,
        **_teardown_kwargs,
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

    gate_timed_out = bool(timed_out or output.startswith("TIMEOUT"))
    if facts_out is not None:
        facts_out.append(
            GateRunFacts(
                timed_out=gate_timed_out,
                timeout_s=gate_timeout,
                command=gate_cmd,
                exit_code=exit_code,
            )
        )

    if gate_timed_out:
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
    process_teardowns: list[ProcessTeardown] | None = None,
) -> GateDebugTelemetry | None:
    """Run the configured gate debug command after a gate timeout.

    ``process_teardowns`` collects any forced teardown, exactly as on the main
    gate path: this command runs after a gate *timeout*, so it is if anything
    more likely than the gate itself to be racing something that is still alive.
    """
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
        teardown_out=process_teardowns,
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
    selection_out: list[SelectedValidation] | None = None,
    facts_out: list[GateRunFacts] | None = None,
) -> tuple[str | None, str | None, str]:
    """Run the gate command. Returns (decision, error, output_tail).

    ``selection_out`` is the same out-parameter ``run_gate_full`` takes, passed
    through so this wrapper's callers — resume triage and post-conflict
    verification, both of which act on the result — can say which profile
    produced it and what authority it carried (#2358). Without the passthrough
    those two runs were the only coordinator-executed validation whose standing
    left no trace at all.

    ``facts_out`` is passed through for the same reason: resume triage acts on
    the result, and a gate that was killed at its budget asks for different work
    than one whose tests failed (#2796).
    """
    decision, error, output_tail, _resolved_cmd, _exit_code = run_gate_full(
        config,
        workspace_path,
        task,
        label=label,
        selection_out=selection_out,
        facts_out=facts_out,
    )
    return decision, error, output_tail
