"""Shared test fixtures and helpers for coordinator test modules."""

import time as _time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch  # noqa: F401 — re-exported for convenience

import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    NotificationConfig,
    NtfyConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.runners import AgentResult, LogLevel  # noqa: F401
from theforge.task import TaskStory


def _make_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config pointing at tmp_path (single reviewer, no synthesis)."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_pool_config(
    tmp_path: Path, profiles: list[ModelProfile], synthesis: ModelProfile
) -> ForgeConfig:
    """Create a test config with multi-model review pool."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=profiles,
        synthesis_profile=synthesis,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path) -> TaskStory:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


def _make_agent_result(
    success: bool = True,
    output: str = "Done.",
    session_id: str | None = "sess-1",
    cost_usd: float = 0.50,
    profile_name: str = "",
    startup_failure: bool = False,
    dev_handoff: dict | None = None,
    failure_code: str | None = None,
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=session_id,
        cost_usd=cost_usd,
        exit_code=0 if success else 1,
        raw={},
        profile_name=profile_name,
        startup_failure=startup_failure,
        dev_handoff=dev_handoff,
        failure_code=failure_code,
    )


def _make_pool_result(
    outputs: list[str],
    profile_names: list[str],
    success: bool = True,
    cost_usd: float = 0.20,
) -> list[AgentResult]:
    """Build a list of AgentResults as if returned by run_agent_pool."""
    return [
        AgentResult(
            success=success,
            output=out,
            session_id=None,
            cost_usd=cost_usd,
            exit_code=0 if success else 1,
            raw={},
            profile_name=name,
        )
        for out, name in zip(outputs, profile_names)
    ]


_VALID_DEV_NOTES = (
    'summary: "Implemented the feature."\n'
    "commits:\n"
    '  - sha: "abc1234"\n'
    '    message: "feat: implement"\n'
    "acceptance_criteria:\n"
    '  - criterion: "It works"\n'
    "    status: MET\n"
    '    notes: "tested"\n'
    "story_deviations: none\n"
    "deferred_items: none\n"
    "gate_result: PASS\n"
)


def _write_handoff(
    workspace: Path,
    decision: str = "PASS",
    handoff_file: str = "handoff.yaml",
    *,
    dev_notes: str = _VALID_DEV_NOTES,
) -> None:
    """Write a minimal handoff file in the workspace with valid dev_notes."""
    handoff = {
        "gate_decision": decision,
        "validation": {"make_fmt": {"status": "PASS"}},
        "scope_completed": ["test item"],
        "dev_notes": dev_notes,
    }
    handoff_path = workspace / handoff_file
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(yaml.dump(handoff), encoding="utf-8")


# Stale-worktree detection commands need specific responses so that pre-created
# workspaces in existing tests are treated as "fresh" (reused rather than removed).
_RECENT_COMMIT_TS = str(int(_time.time()) - 60)  # 1 minute ago


def _handle_stale_check_cmd(cmd: str) -> tuple[bool, str] | None:
    """Return a mock response for stale-worktree detection git commands, or None if not matched."""
    if "rev-parse --abbrev-ref HEAD" in cmd:
        return (True, "forge/test-task")
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "abc1234 feat: implement")
    if "--format=%ct" in cmd:
        return (True, _RECENT_COMMIT_TS)
    return None


# ── Sanctioned gate/shell test seam ──────────────────────────────────────────
#
# Production gate execution has a single path: gate.py and gate_diagnostics.py
# call ``theforge.coordinator.util._run_shell_detailed`` directly (see #1737).
# There is no ``isinstance(_, Mock)`` branch in production that varies behavior
# under test. Tests that need to simulate gate/shell behavior do so through this
# one shared seam, which patches that single primitive. Because ``_run_shell``
# delegates to ``_run_shell_detailed``, patching the detailed primitive also
# covers git-op callers (git add/commit/status) via delegation — this seam is the
# one place that owns the command dispatch and the 2-tuple→4-tuple adaptation.


def _as_detailed(side_effect):
    """Adapt a 2-tuple ``_run_shell`` side_effect/callable to the 4-tuple
    ``_run_shell_detailed`` contract this seam patches.

    Exit code and timeout are *derived* here — ``0`` on success / ``1`` on
    failure, ``timed_out`` when the output carries the ``TIMEOUT`` banner — the
    same derivation the old production Mock-branch performed, but now living in
    test support instead of shipped code. This is the *convenience* adapter for
    flow tests that only care about PASS/FAIL. Gate-focused tests that must
    assert *observed* exit codes / timeout state use :func:`mock_gate` with
    explicit ``exit_code=``/``timed_out=`` instead of this derivation.

    A callable that already returns a 4-tuple is passed through unchanged, so
    fidelity side_effects can be adapted harmlessly.
    """

    def detailed(cmd, cwd, **kwargs):
        result = side_effect(cmd, cwd, **kwargs)
        if len(result) == 4:
            return result
        ok, output = result
        return ok, output, (0 if ok else 1), output.startswith("TIMEOUT")

    return detailed


def _gate_side_effect(
    workspace: Path,
    decisions: list[str] | str = "PASS",
    *,
    exit_code: int | None = None,
    timed_out: bool = False,
    output: str | None = None,
):
    """Build a 4-tuple ``_run_shell_detailed`` side_effect that simulates gate
    execution, git-status (clean worktree), and stale-worktree checks by command.

    Convenience mode (``exit_code=None``): the gate 4-tuple is derived from the
    PASS/FAIL decision (``0``/``1``, never timed out).

    Fidelity mode (``exit_code`` given, and/or ``timed_out=True``): gate calls
    return the *specified* observed values so exit-code/timeout suites assert
    what the subprocess reported, not a derived ``0 if ok else 1``.
    """
    if isinstance(decisions, str):
        decisions_list = [decisions] * 20
    else:
        decisions_list = list(decisions)
    gate_idx = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if "gate" in cmd:
            d = decisions_list[min(gate_idx["n"], len(decisions_list) - 1)]
            gate_idx["n"] += 1
            ok = d == "PASS"
            # Gate pass/fail is determined by exit code; write dev_notes on PASS
            # so handoff validation succeeds.
            if ok:
                _write_handoff(Path(cwd), d)
                gate_out = output if output is not None else "OK"
            else:
                gate_out = output if output is not None else "FAIL: tests failed"
            code = exit_code if exit_code is not None else (0 if ok else 1)
            return (ok, gate_out, code, timed_out)
        if "git status --porcelain" in cmd:
            return (True, "", 0, False)  # clean worktree
        stale_resp = _handle_stale_check_cmd(cmd)
        if stale_resp is not None:
            ok, out = stale_resp
            return (ok, out, 0 if ok else 1, False)
        return (True, "OK", 0, False)

    return side_effect


def _shell_with_gate(workspace: Path, decisions: list[str] | str = "PASS"):
    """Legacy convenience seam: a gate/shell side_effect for ``_run_shell_detailed``.

    Re-expressed atop :func:`_gate_side_effect` so the dispatch and exit-code
    derivation live in one place. Apply it to
    ``theforge.coordinator.util._run_shell_detailed`` (the single primitive the
    production gate path calls); git-op callers reach it via ``_run_shell``
    delegation. Kept for flow tests whose behavior must be unchanged.
    """
    return _gate_side_effect(workspace, decisions)


@contextmanager
def mock_gate(
    workspace: Path,
    decisions: list[str] | str = "PASS",
    *,
    exit_code: int | None = None,
    timed_out: bool = False,
    output: str | None = None,
):
    """The single sanctioned seam for simulating gate/shell execution in tests.

    Patches ``theforge.coordinator.util._run_shell_detailed`` — the one primitive
    the cleaned production gate path calls — dispatching by command (gate command,
    git status, stale-worktree checks, other git ops). git-op callers that use
    ``_run_shell`` reach the same patch through delegation.

    Two modes:

    * **Convenience** — pass a ``PASS``/``FAIL`` decision (or a list of decisions);
      a default ``exit_code`` (``0``/``1``) and ``timed_out=False`` are derived,
      for flow tests that only care about the decision.
    * **Fidelity** — pass explicit ``exit_code=``/``timed_out=`` so gate-focused
      tests specify and assert the *observed* 4-tuple values rather than a
      derived ``0 if ok else 1``.

    Yields the underlying ``Mock`` so tests can assert on the primitive's calls.
    """
    side_effect = _gate_side_effect(
        workspace,
        decisions,
        exit_code=exit_code,
        timed_out=timed_out,
        output=output,
    )
    with patch(
        "theforge.coordinator.util._run_shell_detailed", side_effect=side_effect
    ) as mock_shell:
        yield mock_shell


APPROVE_REVIEW = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
ac_verification:
  - criterion: "Implementation satisfies the spec"
    status: VERIFIED
    evidence: "diff hunks present and tests cover the failure mode (test fixture default)"
```
"""

REQUEST_CHANGES_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Bug found."
findings:
  - severity: P1
    file: src/foo.py
    line: 10
    observed: "Off by one"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix it"
story_compliance:
  matches_spec: false
  mismatches:
    - "Missing batch config"
test_coverage:
  adequate: false
  gaps:
    - "No edge case test"
```
"""

PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Spec requirements are not yet implemented."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

PREFLIGHT_ALREADY_DONE = """\
```yaml
verdict: ALREADY_DONE
reason: "All acceptance criteria are already satisfied."
criteria_checked:
  - criterion: "Feature X"
    satisfied: true
    files_checked:
      - "coordinator.py"
    runtime_path: "run_task() -> _run_preflight_phase() -> coordinator execution path"
    evidence: >-
      The live coordinator path executes coordinator.py and produces the
      expected observable behavior for Feature X.
```
"""

PREFLIGHT_BLOCKED = """\
```yaml
verdict: BLOCKED
reason: "Spec references removed_function() which no longer exists."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "removed_function() was deleted in a prior commit"
```
"""

PREFLIGHT_PROCEED_WITH_WARNINGS = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Spec requirements are not yet implemented."
warnings:
  - "src/theforge/old_module.py does not exist on disk"
  - "src/theforge/another_missing.py does not exist on disk"
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""


_PREFLIGHT_RESULT = _make_agent_result(
    success=True, output=PREFLIGHT_PROCEED, cost_usd=0.05, profile_name="review"
)


def _preflight_then(*dev_results: AgentResult):
    """Preflight PROCEED on first call, then dev_results."""
    preflight_result = _PREFLIGHT_RESULT
    results = [preflight_result, *dev_results]
    call_idx = {"n": 0}

    def side_effect(**kwargs):
        idx = min(call_idx["n"], len(results) - 1)
        call_idx["n"] += 1
        return results[idx]

    return side_effect


def _make_ntfy_config(tmp_path: Path, timeout: int = 60) -> ForgeConfig:
    """Create a ForgeConfig with ntfy notifications enabled."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2, escalate_policy="reject"),
        notifications=NotificationConfig(
            backend="ntfy",
            ntfy=NtfyConfig(url="https://ntfy.sh/test-topic", priority="default"),
            human_review_timeout_seconds=timeout,
        ),
        log=LogConfig(enabled=False),
    )


def _make_review_profile(name: str, budget_usd: float = 1.0) -> ModelProfile:
    return ModelProfile(
        name=name,
        cli="claude",
        model="opus",
        budget_usd=budget_usd,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


SYNTHESIS_PROFILE = ModelProfile(
    name="synthesis",
    cli="claude",
    model="opus",
    budget_usd=1.50,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash"),
)


PARSE_ERROR_OUTPUT = "this is not valid yaml: {{{ completely broken"

SCHEMA_ERROR_OUTPUT = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
```
"""


PREFLIGHT_PROCEED_MEDIUM = """\
```yaml
verdict: PROCEED
complexity: medium
reason: "Spec requirements are not yet implemented."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

PREFLIGHT_PROCEED_SMALL = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Small config change needed."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""


def _make_plan_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config with PLAN phase enabled."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
    )
