"""Stack-specific gate-timeout diagnostic re-run pass (issue #1217).

This module lives intentionally OUTSIDE TheForge's stack-neutral core
(``coordinator/``, ``task/``, ``sprint/``): it encodes pytest-specific
instrumentation — serialized execution (``-n 0``), a hard per-test timeout,
thread-method stack dumps, and faulthandler frames — used to isolate a hanging
test after the validation gate times out. Core routing calls
:func:`run_gate_diagnostic_pass`; the tool-specific knowledge (the default
command and the output parsing) is quarantined here so the coordinator stays
tool-agnostic. Operators may override the command entirely via
``validation.gate_diagnostic_command`` in forge.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.coordinator import util as _cu
from theforge.coordinator.state import GateDiagnosticTelemetry
from theforge.task import TaskStory
from theforge.traces import write_trace
from theforge.workspace_env import build_workspace_env

# Default diagnostic command. ``{per_test_timeout}`` and ``{test_target}`` are
# substituted at call time. Serialized (-n 0) execution surfaces the single
# hanging test by name; the thread-method per-test timeout dumps its stack
# trace; faulthandler (enabled via the environment) adds lower-level frames.
_DEFAULT_DIAGNOSTIC_COMMAND = (
    "python -m pytest -n 0 --timeout={per_test_timeout} --timeout-method=thread {test_target}"
)

# pytest-timeout emits the offending node id in its per-test timeout banner and
# again in the terminal summary. The summary line is the most reliable, so it is
# scanned first.
_TIMEOUT_SUMMARY_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<node>\S+::\S+)\b.*?Timeout", re.MULTILINE
)
_TIMEOUT_BANNER_RE = re.compile(r"(?P<node>\S+::\S+)\s*\n[+~]+\s*Timeout", re.MULTILINE)


def extract_hanging_test(output: str) -> str | None:
    """Best-effort extraction of the single test node that exceeded the per-test timeout.

    Returns the node id (e.g. ``tests/test_x.py::test_hang``) when exactly one
    test is implicated by the diagnostic output, else None. When more than one
    distinct node timed out, returns None: the "single hanging test" highlight in
    the dev retry packet is only meaningful when the pass isolated one.
    """
    nodes: list[str] = []
    for hit in _TIMEOUT_SUMMARY_RE.finditer(output):
        nodes.append(hit.group("node"))
    if not nodes:
        for hit in _TIMEOUT_BANNER_RE.finditer(output):
            nodes.append(hit.group("node"))
    distinct = list(dict.fromkeys(nodes))
    if len(distinct) == 1:
        return distinct[0]
    return None


def _resolve_test_target(config: ForgeConfig, task: TaskStory | None) -> str:
    """Resolve the test target for the diagnostic pass (task override or default)."""
    default_target = config.validation.default_test_target or "."
    return (task.test_target if task is not None else None) or default_target


def run_gate_diagnostic_pass(
    config: ForgeConfig,
    workspace_path: Path,
    *,
    task: TaskStory | None,
    iter_num: int,
) -> GateDiagnosticTelemetry | None:
    """Run a serialized diagnostic pass after a gate timeout (issue #1217).

    Runs the resolved diagnostic command with a hard wall-clock budget so it
    cannot itself become a wall-clock failure. The original gate process group is
    already killed by ``_run_shell_detailed`` on timeout before this runs.
    faulthandler is enabled via the environment so lower-level frames appear
    alongside the per-test-timeout stack dump.

    Returns telemetry (written to the audit + a trace file) or None when the
    diagnostic pass is disabled.
    """
    val = config.validation
    if not val.gate_diagnostic_enabled:
        return None

    per_test_timeout = val.gate_diagnostic_per_test_timeout
    budget = val.gate_diagnostic_budget
    test_target = _resolve_test_target(config, task)
    command_template = val.gate_diagnostic_command or _DEFAULT_DIAGNOSTIC_COMMAND
    diagnostic_cmd = command_template.format(
        per_test_timeout=per_test_timeout,
        test_target=test_target,
    )
    _cu._log(f"  Running gate diagnostic pass after timeout: {diagnostic_cmd}")

    env = build_workspace_env(
        workspace_path,
        extra={"PYTHONFAULTHANDLER": "1"},
        expected_python=config.workspace.python_interpreter,
    )
    _ok, output, exit_code, timed_out = _cu._run_shell_detailed(
        diagnostic_cmd,
        workspace_path,
        timeout=budget,
        env=env,
    )

    if timed_out:
        _cu._log(f"  Gate diagnostic pass hit its {budget}s budget before finishing")
    hanging_test = extract_hanging_test(output)
    if hanging_test:
        _cu._log(f"  Gate diagnostic pass isolated hanging test: {hanging_test}")

    tail_chars = val.gate_output_tail_chars
    output_tail = output[-tail_chars:]
    write_trace(
        workspace_path / ".forge/traces" / f"{iter_num}-gate-diagnostic.txt",
        output,
    )
    return GateDiagnosticTelemetry(
        iteration=iter_num,
        command=diagnostic_cmd,
        ran=True,
        budget_s=budget,
        per_test_timeout_s=per_test_timeout,
        exit_code=exit_code,
        timed_out=timed_out,
        hanging_test=hanging_test,
        output_tail=output_tail,
        output_truncated=len(output) > len(output_tail),
    )
