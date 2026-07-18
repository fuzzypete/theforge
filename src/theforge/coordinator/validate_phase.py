"""VALIDATE phase handler: gate execution, dirty-worktree auto-commit, retry/escalate routing."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import subprocess
import time
import types
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

from theforge.advisory_conventions import update_advisory_violations
from theforge.config import ForgeConfig
from theforge.gate_diagnostics import run_gate_diagnostic_pass
from theforge.task import TaskStory

from . import util as _cu
from .commit_guard import _commits_exist_strict, _has_commits_ahead_of_base
from .dev_phase import extract_failed_tests, record_dev_iteration_telemetry
from .gate import (
    _is_gate_skip,
    _parse_dirty_files,
    _run_gate_debug_command,
    format_gate_failure_summary,
    run_gate_full,
)
from .logging import StructuredLogger
from .notify import _escalate_notify
from .review_context import (
    _get_handoff_content,
    _get_raw_dev_notes,
    _latest_forge_handoff_path,
    _parse_dev_handoff,
)
from .state import (
    CoordinatorResult,
    CoordinatorState,
    DevIterationTelemetry,
    GateDiagnosticTelemetry,
    Phase,
    RetryReason,
)
from .util import _log, _log_phase, _log_verbose
from .workspace import _deindex_forge_artifacts


class _ValidateOutcome(Enum):
    PASS = auto()
    RETRY_DEV = auto()
    REVIEW_CONVENTION_BLOCK = auto()
    ESCALATE = auto()
    ALREADY_COMPLETE = auto()


def _verified_handoff_commit_shas(
    workspace_path: Path, shas: list[str], base_branch: str
) -> list[str]:
    """Return the subset of ``shas`` reachable from HEAD or the base branch.

    A commit SHA is "verified" only when ``git merge-base --is-ancestor`` shows
    it is reachable from this worktree's HEAD or from the configured base
    branch (preferring ``origin/<base>``, falling back to the local ref). This
    is a stronger check than object existence: a handoff that cites a SHA from
    another local branch or a fetched-but-unreachable ref must not unlock the
    ALREADY_COMPLETE success path, because the cited work is not actually on
    the target branch.
    """
    refs_to_check = ["HEAD", f"origin/{base_branch}", base_branch]
    verified: list[str] = []
    for sha in shas:
        sha = sha.strip()
        if not sha:
            continue
        reachable = False
        for ref in refs_to_check:
            try:
                proc = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", sha, ref],
                    cwd=str(workspace_path),
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except Exception:  # noqa: BLE001
                continue
            if proc.returncode == 0:
                reachable = True
                break
        if reachable:
            verified.append(sha)
    return verified


def _handoff_documents_already_complete(
    workspace_path: Path,
    forge_handoff_path: Path | None,
    base_branch: str,
) -> tuple[bool, list[dict], str | None]:
    """Decide whether the dev handoff explicitly documents 'work already complete'.

    Returns ``(is_already_complete, verified_commits, reason)``. The first
    element is True only when:
    - the handoff parsed cleanly (no schema or parse errors),
    - it lists at least one acceptance criterion,
    - every acceptance criterion has status normalized to ``MET``,
    - at least one commit SHA cited in the handoff is reachable from HEAD or
      the base branch (citation verification — protects against fabricated
      SHAs and against SHAs on unrelated branches that are not actually part
      of this branch's history).
    """
    handoff = _parse_dev_handoff(forge_handoff_path=forge_handoff_path)
    if handoff is None:
        return False, [], "no handoff artifact"
    if handoff.parse_errors:
        return False, [], f"handoff parse errors: {handoff.parse_errors[0]}"
    if not handoff.acceptance_criteria:
        return False, [], "handoff has no acceptance_criteria"
    for ac in handoff.acceptance_criteria:
        status = (ac.get("status") or "").strip().upper()
        if status != "MET":
            return False, [], f"acceptance criterion status is {status!r}, not MET"
    cited_shas = [c.get("sha", "") for c in handoff.commits]
    verified = _verified_handoff_commit_shas(workspace_path, cited_shas, base_branch)
    if not verified:
        return False, [], "handoff cites no commit SHA reachable from HEAD or base branch"
    verified_set = set(verified)
    verified_commits = [
        {"sha": c.get("sha", "").strip(), "message": c.get("message", "")}
        for c in handoff.commits
        if c.get("sha", "").strip() in verified_set
    ]
    return True, verified_commits, None


def _is_identical_failure(telemetry: list[DevIterationTelemetry]) -> bool:
    """Return True if the last two recorded iterations share an identical failure signature.

    Two failure signatures are identical when:
    - Both iterations timed out, OR
    - Both have the same non-empty set of failing tests.
    """
    if len(telemetry) < 2:
        return False
    prev = telemetry[-2]
    curr = telemetry[-1]
    if curr.is_timeout and prev.is_timeout:
        return True
    if curr.failed_tests and set(curr.failed_tests) == set(prev.failed_tests):
        return True
    return False


def _gate_trace_path(iter_num: int | None) -> str | None:
    """Return the worktree-relative gate trace path written for this iteration.

    Mirrors ``run_gate_full``'s trace-write gate: a trace is persisted only when
    ``iter_num is not None``. Baseline gate runs (``iter_num is None``) produce
    no trace, so no artifact is named and the terminal outcome falls back to its
    inline tail. The path is relative to the worktree root
    (``.forge/traces/{iter}-gate.txt``); ESCALATE worktrees are preserved, so it
    remains resolvable for the surfaces that read an escalation.
    """
    if iter_num is None:
        return None
    return f".forge/traces/{iter_num}-gate.txt"


def _test_file_exists_in_head(workspace_path: Path, test_file: str) -> bool:
    """Return whether the failing test file exists in the current checkout."""
    return workspace_path.joinpath(test_file).is_file()


_UNRECOGNIZED_GATE_FORMAT_NOTE = (
    "\n\nNo failing-test identifiers could be extracted from this gate's output:"
    " its format is not recognized by core's built-in (pytest) extractor and no"
    " `validation.failed_test_pattern` is configured in forge.yaml. This retry is"
    " proceeding without a focused failing-test list — read the full gate output"
    " above to find the failures yourself, and consider configuring"
    " `failed_test_pattern` so future retries are pointed at the exact tests your"
    " gate names."
)


def _format_failed_test_feedback(
    gate_output_tail: str,
    workspace_path: Path,
    contract_change: bool = False,
    failed_test_pattern: str | None = None,
) -> tuple[str, bool]:
    """Return retry-feedback text for extracted failing tests and whether they are existing.

    When extraction does not apply (the gate output is in a format core does not
    recognize and no ``failed_test_pattern`` is configured), the returned text
    carries an explicit note so the degradation is visible to the dev retry
    rather than reading identically to a genuine no-test-failure gate error.
    """
    extraction = extract_failed_tests(gate_output_tail, failed_test_pattern)
    failed_tests = extraction.tests
    if not failed_tests:
        if not extraction.format_recognized:
            return _UNRECOGNIZED_GATE_FORMAT_NOTE, False
        return "", False

    existing_failures = [
        test_name
        for test_name in failed_tests
        if _test_file_exists_in_head(workspace_path, test_name.split("::", 1)[0])
    ]
    lines = ["\n\nExtracted failing tests (best effort):"]
    lines.extend(f"- {test_name}" for test_name in failed_tests)
    if existing_failures:
        if contract_change:
            lines.append(
                "Some of these tests may assert the old behavioral contract — "
                "update them if they encode the behavior this story is changing."
            )
        else:
            lines.append(
                "These are existing tests your changes broke — "
                "fix your implementation, do not edit these test files."
            )
    return "\n".join(lines), bool(existing_failures)


def _check_conventions_parallel(
    config: ForgeConfig,
    workspace_path: Path,
) -> tuple[list, list] | None:
    """Return (all_violations, net_new_violations) or None if conventions are not configured.

    Designed to run in a thread concurrently with gate execution.
    Gate commands are test runners; they don't write source files.
    Convention scanner reads source, not test output. Parallel execution is safe.
    """
    if config.conventions_hard is None:
        return None
    _config_dict = dataclasses.asdict(config.conventions_hard)
    baseline_ref = _get_convention_baseline_ref(workspace_path, config.workspace.base_branch)
    if baseline_ref is not None:
        result = _cu._run_worktree_eval(
            workspace_path,
            "check_conventions",
            {
                "config": _config_dict,
                "project_root": str(workspace_path),
                "baseline_ref": baseline_ref,
            },
        )
        all_v = [types.SimpleNamespace(**d) for d in result["all_violations"]]
        net_v = [types.SimpleNamespace(**d) for d in result["violations"]]
    else:
        result = _cu._run_worktree_eval(
            workspace_path,
            "check_conventions",
            {"config": _config_dict, "project_root": str(workspace_path)},
        )
        all_v = [types.SimpleNamespace(**d) for d in result["violations"]]
        net_v = all_v
    return all_v, net_v


def _record_advisory_convention_state(
    config: ForgeConfig,
    task: TaskStory,
    state: CoordinatorState,
    violations: list[dict],
    logger: StructuredLogger | None,
) -> None:
    """Persist rolling advisory convention state for the current scan."""
    advisory_result = update_advisory_violations(
        config,
        violations,
        observed_at=dt.datetime.now(dt.timezone.utc),
        run_id=state.run_id,
        story_slug=task.slug,
    )
    if logger:
        logger._safe_emit(
            "convention_advisory_state",
            artifact_path=advisory_result["path"],
            entry_count=advisory_result["entry_count"],
            newly_filed_issues=advisory_result["newly_filed_issues"],
        )


def _build_timeout_rca_packet(
    *,
    state: CoordinatorState,
    config: ForgeConfig,
    gate_cmd: str,
    gate_output_tail: str,
    gate_err: str | None,
    workspace_path: Path,
    diagnostic: GateDiagnosticTelemetry | None = None,
) -> str:
    """Assemble the dev retry input for a gate-timeout-with-commits failure."""
    gate_timeout_s = config.validation.gate_timeout or 600
    tail_chars = config.validation.gate_output_tail_chars

    head_sha = "(unknown)"
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            head_sha = proc.stdout.decode().strip() or "(unknown)"
    except Exception:  # noqa: BLE001
        pass

    commits_log = ""
    try:
        log_proc = subprocess.run(
            [
                "git",
                "log",
                "--oneline",
                f"{config.workspace.base_branch}..HEAD",
            ],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
            check=False,
        )
        if log_proc.returncode == 0:
            commits_log = log_proc.stdout.decode(errors="replace").strip()
    except Exception:  # noqa: BLE001
        pass

    handoff_text = _get_handoff_content(forge_handoff_path=_latest_forge_handoff_path(state))

    parts = [
        f"The validation gate (`{gate_cmd}`) timed out after {gate_timeout_s}s while running"
        " over your diff. Treat this as a normal dev failure: your diff almost certainly"
        " caused the hang. RCA which test or product-code path is hanging, fix the underlying"
        " bug, and re-run the gate. Do not increase the timeout or delete coverage as the"
        " first move — the wall-clock guard is intentional.",
        f"Configured gate_timeout: {gate_timeout_s}s",
        f"HEAD commit under test: {head_sha}",
    ]
    if commits_log:
        parts.append(f"Commits ahead of base:\n{commits_log}")
    parts.append(
        f"Gate output tail (last {tail_chars} chars; may be truncated at the kill"
        f" boundary):\n{gate_output_tail or gate_err or '(empty)'}"
    )
    if state.gate_debug_telemetry:
        dbg = state.gate_debug_telemetry[-1]
        parts.append(
            f"Gate debug command (`{dbg.command}`) exit={dbg.exit_code}"
            f" timeout={dbg.timeout_s}s output tail:\n{dbg.output_tail}"
        )
    if diagnostic is not None:
        diag_lines = [
            f"Diagnostic re-run (`{diagnostic.command}`): serialized single-worker execution"
            f" with a {diagnostic.per_test_timeout_s}s hard per-test timeout, bounded to"
            f" {diagnostic.budget_s}s total. A per-test timeout dumps the stack trace at the"
            " moment of the hang; faulthandler adds lower-level frames if relevant."
        ]
        if diagnostic.hanging_test:
            diag_lines.append(
                f">>> Hanging test isolated: {diagnostic.hanging_test} — start here."
                " The serialized run named this test as the one that exceeded the per-test"
                " timeout. Its stack trace is in the diagnostic output below."
            )
        elif diagnostic.timed_out:
            diag_lines.append(
                "The diagnostic pass itself hit its time budget before finishing, so no single"
                " test could be isolated. The hang may be spread across setup/collection or be"
                " concurrency-specific."
            )
        else:
            diag_lines.append(
                "No single test exceeded the per-test timeout under serialized execution."
                " This suggests a concurrency-specific bug: the hang only reproduces under"
                " parallel execution, not when tests run one at a time."
            )
        diag_lines.append(f"Diagnostic output tail:\n{diagnostic.output_tail or '(empty)'}")
        parts.append("\n".join(diag_lines))
    parts.append(f"Current handoff:\n{handoff_text}")
    return "\n\n".join(parts)


def _run_validate_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    notify: bool,
    logger: StructuredLogger | None,
    state_update_fn: Callable[[dict], None] | None = None,
) -> tuple[_ValidateOutcome, CoordinatorResult | None]:
    """Run one VALIDATE iteration. Returns (outcome, result).

    ESCALATE returns (ESCALATE, CoordinatorResult). RETRY_DEV and PASS return (outcome, None).
    Does NOT emit 'phase_end VALIDATE pass' — caller emits that (it fires on skip too).
    """
    state.phase = Phase.VALIDATE
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "VALIDATE",
                "iteration": state.dev_iteration,
                "cost_usd": state.total_cost,
            }
        )
    if logger:
        logger._safe_emit("phase_start", phase="VALIDATE", iteration=state.dev_iteration)

    # Submit convention check to run in parallel with gate execution.
    # Gate commands are test runners; they don't write source files. Convention
    # scanner reads source, not test output. Parallel execution is safe.
    _cv_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _cv_future: concurrent.futures.Future = _cv_executor.submit(
        _check_conventions_parallel, config, workspace_path
    )

    _gate_start = time.monotonic()
    gate_override = task.gate_override
    gate_output_tail: str = ""
    gate_exit_code: int | None = None
    gate_result_for_telemetry: str | None = None
    if _is_gate_skip(gate_override):
        _log_phase(state.phase, "skipped (gate: none)")
        _log("  Gate: none (story override)")
        gate_decision: str | None = "PASS"
        gate_err: str | None = None
        gate_result_for_telemetry = gate_decision
    else:
        if gate_override is not None:
            _log_phase(state.phase, "running gate... (override)")
            _log(f"  Gate: {gate_override} (story override)")
        else:
            _log_phase(state.phase, "running gate...")
        gate_decision, gate_err, gate_output_tail, resolved_gate_cmd, gate_exit_code = (
            run_gate_full(config, workspace_path, task=task, iter_num=state.dev_iteration)
        )
        gate_result_for_telemetry = gate_decision or "ERROR"
    _gate_elapsed = time.monotonic() - _gate_start
    state.validate_durations.append(_gate_elapsed)
    if logger:
        logger._safe_emit(
            "gate_result",
            decision=gate_decision or gate_err,
            duration_s=round(_gate_elapsed, 2),
            output_tail=gate_output_tail[-500:] if gate_output_tail else "",
        )

    # Retrieve convention check result (ran in parallel; should be done by now).
    try:
        _cv_result_raw = _cv_future.result(timeout=120)
    except Exception as exc:
        _log(f"  ⚠ Convention check failed or timed out: {exc}")
        _cv_result_raw = None
    finally:
        _cv_executor.shutdown(wait=False)

    _cv_all: list = _cv_result_raw[0] if _cv_result_raw is not None else []
    _cv_violations: list = _cv_result_raw[1] if _cv_result_raw is not None else []
    state.convention_violations = [
        {"rule": v.rule, "file": v.file, "detail": v.detail, "blocking": v.blocking}
        for v in _cv_violations
    ]
    if _cv_result_raw is not None:
        _record_advisory_convention_state(
            config,
            task,
            state,
            [
                {"rule": v.rule, "file": v.file, "detail": v.detail, "blocking": v.blocking}
                for v in _cv_all
            ],
            logger,
        )

    if gate_err:
        is_timeout = "timed out" in (gate_err or "").lower()
        if is_timeout:
            debug_telemetry = _run_gate_debug_command(
                config,
                workspace_path,
                iter_num=state.dev_iteration,
            )
            if debug_telemetry is not None:
                state.gate_debug_telemetry.append(debug_telemetry)
                gate_err = (
                    f"{gate_err}. Gate debug command ran; see audit "
                    f"iterations.gate_debug[-1] and trace "
                    f".forge/traces/{state.dev_iteration}-gate-debug.txt."
                    f"\nGate debug output tail:\n{debug_telemetry.output_tail}"
                )
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result=gate_result_for_telemetry,
            gate_output_tail=gate_output_tail or gate_err,
            is_timeout=is_timeout,
            failed_test_pattern=config.validation.failed_test_pattern,
        )
        # Timeout with dev commits is a retryable validation failure: hand back
        # to dev with a timeout-RCA evidence packet rather than escalating
        # terminally. Routes only when (a) the gate actually timed out, (b) the
        # dev iteration produced commits ahead of base, (c) budget remains, and
        # (d) the failure is not a consecutive-identical timeout (circuit
        # breaker still owns that case). Infrastructure errors (state 4) and
        # empty-worktree timeouts (state 1) continue to escalate.
        if (
            is_timeout
            and not _is_identical_failure(state.dev_iteration_telemetry)
            and not state.budget.is_exhausted()
            and _commits_exist_strict(workspace_path, config.workspace.base_branch)
        ):
            # The original gate process group was already killed by
            # _run_shell_detailed on timeout (spec step 1). Run the diagnostic
            # re-run pass in the same worktree before constructing the retry
            # input so the dev agent gets the hanging test + stack trace on the
            # first retry rather than having to re-run the suite manually.
            diagnostic = run_gate_diagnostic_pass(
                config,
                workspace_path,
                task=task,
                iter_num=state.dev_iteration,
            )
            if diagnostic is not None:
                state.gate_diagnostic_telemetry.append(diagnostic)
                if logger:
                    logger._safe_emit(
                        "gate_diagnostic",
                        iteration=diagnostic.iteration,
                        command=diagnostic.command,
                        exit_code=diagnostic.exit_code,
                        timed_out=diagnostic.timed_out,
                        hanging_test=diagnostic.hanging_test,
                        output_tail=diagnostic.output_tail[-500:],
                    )
            state.human_feedback = _build_timeout_rca_packet(
                state=state,
                config=config,
                gate_cmd=resolved_gate_cmd,
                gate_output_tail=gate_output_tail,
                gate_err=gate_err,
                workspace_path=workspace_path,
                diagnostic=diagnostic,
            )
            state.retry_reason = RetryReason.GATE_FAIL
            _log(
                f"  ✗ VALIDATE   TIMEOUT  (iter={state.dev_iteration}"
                " → retrying dev with RCA packet)"
            )
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="fail")
            return _ValidateOutcome.RETRY_DEV, None
        # Check consecutive identical failures (including timeouts) before escalating.
        if _is_identical_failure(state.dev_iteration_telemetry):
            state.phase = Phase.ESCALATE
            state.error = (
                f"Identical gate failure on consecutive iterations"
                f" (iteration {state.dev_iteration}): gate error: {gate_err}."
                f" Remaining retry budget: {state.budget.remaining()}."
            )
        else:
            state.phase = Phase.ESCALATE
            state.error = gate_err
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
        _escalate_notify(task, state, notify, config)
        return _ValidateOutcome.ESCALATE, CoordinatorResult(
            success=False, phase=state.phase, state=state, message=state.error
        )

    assert gate_decision is not None
    state.gate_decisions.append(gate_decision)
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "VALIDATE",
                "iteration": state.dev_iteration,
                "cost_usd": state.total_cost,
                "complexity": state.preflight_complexity,
                "detail": {"gate_status": gate_decision},
            }
        )
    _log_verbose(f"Gate decision: {gate_decision}")

    if gate_decision == "PASS":
        _log("  ✓ VALIDATE   PASS")
        pre_validate_cmd = config.validation.pre_validate_command
        if pre_validate_cmd:
            _log(f"  Running pre-validate command: {pre_validate_cmd}")
            pv_ok, pv_out = _cu._run_shell(pre_validate_cmd, workspace_path)
            if not pv_ok:
                _log(f"  ⚠ Pre-validate command failed (non-fatal): {pv_out[:200]}")
            else:
                _log_verbose(f"Pre-validate output: {pv_out[:200]}")
        # Defensive scrub: if an agent force-added .forge artifacts, remove them
        # from the index before dirty-worktree detection and any auto-commit.
        _deindex_forge_artifacts(workspace_path)
        dirty_ok, dirty_out = _cu._run_shell("git status --porcelain", workspace_path)
        if dirty_ok and dirty_out.strip():
            dirty_files = _parse_dirty_files(dirty_out)
            if dirty_files:
                raw_names = ", ".join(dirty_files)
                _log(f"Dirty worktree detected: {raw_names}")

                # Auto-commit: synthesize message from handoff, don't
                # re-invoke the agent (full-prompt retry burns tokens and
                # times out — the agent already wrote the code).
                dev_notes = _get_raw_dev_notes(
                    forge_handoff_path=_latest_forge_handoff_path(state)
                )
                if dev_notes:
                    first_line = dev_notes.strip().splitlines()[0][:72]
                    commit_msg = first_line
                else:
                    commit_msg = (
                        f"wip: uncommitted changes from dev iteration {state.dev_iteration}"
                    )
                _cu._run_shell("git add -A", workspace_path)
                _deindex_forge_artifacts(workspace_path)
                # Use subprocess.run directly to avoid shell injection
                # from model-authored dev_notes (quotes, backticks, $()).
                try:
                    subprocess.run(
                        ["git", "commit", "-m", commit_msg],
                        cwd=workspace_path,
                        capture_output=True,
                        timeout=30,
                        check=True,
                    )
                    _log(f"  Auto-committed dirty worktree: {commit_msg}")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    err_detail = getattr(exc, "stderr", b"").decode(errors="replace")[:200]
                    _log(f"  ⚠ Auto-commit failed: {err_detail}")
                    # Commit failed — worktree is still dirty.
                    # Escalate: don't let uncommitted changes leak into
                    # REVIEW/DONE/PR.
                    state.phase = Phase.ESCALATE
                    state.error = f"Auto-commit failed for dirty worktree: {raw_names}"
                    _log(f"✗ ESCALATE   {state.error}")
                    _escalate_notify(task, state, notify, config)
                    return _ValidateOutcome.ESCALATE, CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )

        # ── Zero-commits guard ──────────────────────────────────────
        # A trivially passing gate over an empty worktree is *usually* not a real
        # PASS; the default routing treats it as a missing-work failure rather
        # than letting it advance to REVIEW where the empty diff would silently
        # approve. The exception is the deliberate "work already complete"
        # outcome: when the dev cycle inspected the issue and produced a handoff
        # YAML where every acceptance criterion is MET and at least one cited
        # commit SHA resolves in the worktree's git history, the empty diff is
        # the dev cycle's documented contract output and must be classified as a
        # successful ALREADY_DONE-shaped result, not a failure.
        if not _has_commits_ahead_of_base(workspace_path, config.workspace.base_branch):
            handoff_path = _latest_forge_handoff_path(state)
            already_complete, verified_commits, reject_reason = (
                _handoff_documents_already_complete(
                    workspace_path, handoff_path, config.workspace.base_branch
                )
            )
            if already_complete:
                state.validate_already_complete = True
                state.validate_already_complete_commits = verified_commits
                state.validate_already_complete_reason = (
                    "Dev cycle determined no work needed; handoff cites commit"
                    f"(s) {', '.join(c['sha'][:7] for c in verified_commits)}"
                    " already on this branch satisfying all acceptance criteria."
                )
                state.phase = Phase.DONE
                _log(f"  ✓ ALREADY_COMPLETE   {state.validate_already_complete_reason}")
                if logger:
                    logger._safe_emit(
                        "phase_end",
                        phase="VALIDATE",
                        outcome="already_complete",
                        commits=[c["sha"] for c in verified_commits],
                    )
                return _ValidateOutcome.ALREADY_COMPLETE, CoordinatorResult(
                    success=True,
                    phase=state.phase,
                    state=state,
                    message=state.validate_already_complete_reason,
                )
            state.phase = Phase.ESCALATE
            state.error = (
                "Gate exited PASS but branch has no commits ahead of base — "
                "treating empty worktree as missing-work failure"
                f" ({reject_reason})"
                if reject_reason
                else "Gate exited PASS but branch has no commits ahead of base — "
                "treating empty worktree as missing-work failure"
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )

    elif gate_decision in ("FAIL", "BLOCKED"):
        if state.budget.is_exhausted():
            state.phase = Phase.ESCALATE
            state.error = format_gate_failure_summary(
                f"Gate returned {gate_decision} after {state.dev_iteration} attempts",
                exit_code=gate_exit_code,
                output_tail=gate_output_tail,
                tail_chars=config.validation.gate_output_tail_chars,
                trace_path=_gate_trace_path(state.dev_iteration),
            )
            _log(f"✗ ESCALATE   {state.error}")
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result=gate_decision,
                gate_output_tail=gate_output_tail,
                failed_test_pattern=config.validation.failed_test_pattern,
            )
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        handoff_text = _get_handoff_content(forge_handoff_path=_latest_forge_handoff_path(state))
        gate_cmd = resolved_gate_cmd
        tail_chars = config.validation.gate_output_tail_chars
        failed_test_pattern = config.validation.failed_test_pattern
        extraction = extract_failed_tests(gate_output_tail, failed_test_pattern)
        failed_test_feedback, existing_test_failures = _format_failed_test_feedback(
            gate_output_tail,
            workspace_path,
            contract_change=state.preflight_contract_change,
            failed_test_pattern=failed_test_pattern,
        )
        # Surface a silently-inapplicable extraction to the operator and audit
        # trail. An unrecognized gate format yields no failing-test list, which
        # is indistinguishable from a genuine lint/format-only failure unless we
        # say so explicitly here.
        if not extraction.tests and not extraction.format_recognized:
            _log(
                "  ⚠ VALIDATE   gate output format not recognized — no failing"
                " tests extracted; retry runs without a focused test list"
                " (set validation.failed_test_pattern to enable extraction)"
            )
            if logger:
                logger._safe_emit(
                    "failed_test_extraction_skipped",
                    iteration=state.dev_iteration,
                    gate_output_format="unrecognized",
                    reason=(
                        "gate output does not match core's pytest grammar and no"
                        " validation.failed_test_pattern is configured"
                    ),
                )
        state.human_feedback = (
            f"The full test suite (`{gate_cmd}`) failed."
            " Your changes broke something — not just your new tests,"
            " but potentially existing tests too. Run the full suite in"
            " the worktree, find every failure, diagnose the root cause,"
            f" and fix it.{failed_test_feedback}\n\n"
            f"Gate output (last {tail_chars} chars):\n{gate_output_tail}\n\n"
            f"Current handoff:\n{handoff_text}"
        )
        state.retry_reason = RetryReason.GATE_FAIL
        _log(f"  ✗ VALIDATE   {gate_decision}  (iter={state.dev_iteration} → retrying)")
        _log(f"Retrying dev (gate={gate_decision}, iter={state.dev_iteration})")
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result=gate_decision,
            gate_output_tail=gate_output_tail,
            failed_test_pattern=config.validation.failed_test_pattern,
        )
        if state.dev_iteration_telemetry:
            state.dev_iteration_telemetry[-1] = dataclasses.replace(
                state.dev_iteration_telemetry[-1],
                existing_test_failures=existing_test_failures,
            )
        if _is_identical_failure(state.dev_iteration_telemetry):
            state.phase = Phase.ESCALATE
            state.error = format_gate_failure_summary(
                f"Identical gate failure on consecutive iterations"
                f" (iteration {state.dev_iteration}): gate returned {gate_decision}."
                f" Remaining retry budget: {state.budget.remaining()}.",
                exit_code=gate_exit_code,
                output_tail=gate_output_tail,
                tail_chars=config.validation.gate_output_tail_chars,
                trace_path=_gate_trace_path(state.dev_iteration),
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        if _cv_violations:
            _blocking_cv2 = [v for v in _cv_violations if v.blocking]
            _followup_cv2 = [v for v in _cv_violations if not v.blocking]
            if _blocking_cv2:
                lines = [f"  - [{v.rule}] {v.file}: {v.detail}" for v in _blocking_cv2]
                state.human_feedback += (
                    "\n\nAdditionally, hard convention violations were detected:\n"
                    + "\n".join(lines)
                )
            for v in _followup_cv2:
                _log(f"  Convention follow-up [hygiene]: {v.rule} in {v.file} — {v.detail}")
            _log(
                f"  ✗ VALIDATE   convention violations also found"
                f" ({len(_blocking_cv2)} blocking, {len(_followup_cv2)} follow-up)"
            )
        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="fail")
        return _ValidateOutcome.RETRY_DEV, None
    else:
        _log(f"Unknown gate decision: {gate_decision!r}, treating as FAIL")
        state.phase = Phase.ESCALATE
        state.error = f"Unknown gate decision: {gate_decision!r}"
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return _ValidateOutcome.ESCALATE, CoordinatorResult(
            success=False, phase=state.phase, state=state, message=state.error
        )

    record_dev_iteration_telemetry(
        state,
        workspace_path,
        max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
        gate_result=gate_decision,
        gate_output_tail=gate_output_tail,
        failed_test_pattern=config.validation.failed_test_pattern,
    )

    # Convention check ran in parallel with gate; use pre-fetched result.
    # Runs in the worktree's subprocess so self-hosting sprints evaluate the
    # worktree's version of conventions.py, not the coordinator's own copy.
    if config.conventions_hard is not None:
        if _cv_violations:
            blocking_violations = [v for v in _cv_violations if v.blocking]
            followup_violations = [v for v in _cv_violations if not v.blocking]

            # Log and emit follow-up (hygiene) violations — not blocking
            for v in followup_violations:
                _log(f"  Convention follow-up [hygiene]: {v.rule} in {v.file} — {v.detail}")
                if logger:
                    logger._safe_emit(
                        "convention_followup",
                        severity="hygiene",
                        rule=v.rule,
                        file=v.file,
                        detail=v.detail,
                        suggested_story_title=f"Split {v.file} below LOC limit",
                    )

            if blocking_violations:
                lines = [f"  - [{v.rule}] {v.file}: {v.detail}" for v in blocking_violations]
                human_feedback = "Hard convention violations detected:\n" + "\n".join(lines)
                state.human_feedback = human_feedback
                state.retry_reason = RetryReason.CONVENTION_VIOLATIONS
                _log(
                    f"  ✗ VALIDATE   convention violations"
                    f" ({len(blocking_violations)} blocking found)"
                )
                for v in blocking_violations:
                    _log(f"    [{v.rule}] {v.file}: {v.detail}")
                if state.budget.is_exhausted():
                    state.phase = Phase.ESCALATE
                    state.error = (
                        f"Hard convention violations after {state.dev_iteration} attempts"
                    )
                    _log(f"✗ ESCALATE   {state.error}")
                    if logger:
                        logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                        logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
                    _escalate_notify(task, state, notify, config)
                    return _ValidateOutcome.ESCALATE, CoordinatorResult(
                        success=False, phase=state.phase, state=state, message=state.error
                    )
                if logger:
                    logger._safe_emit("phase_end", phase="VALIDATE", outcome="convention_fail")
                return _ValidateOutcome.REVIEW_CONVENTION_BLOCK, None
            # Only follow-up violations — proceed to PASS
        elif _cv_all:
            state.convention_violations = [
                {
                    "rule": v.rule,
                    "file": v.file,
                    "detail": v.detail,
                    "blocking": False,
                }
                for v in _cv_all
            ]
    return _ValidateOutcome.PASS, None


def _get_convention_baseline_ref(workspace_path: Path, base_branch: str) -> str | None:
    """Resolve a git ref representing pre-existing convention debt."""
    try:
        proc = subprocess.run(
            ["git", "merge-base", "HEAD", base_branch],
            cwd=workspace_path,
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    ref = proc.stdout.decode().strip()
    return ref or None
