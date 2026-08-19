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
from typing import NamedTuple

from theforge.advisory_conventions import AdvisoryArtifactError, update_advisory_violations
from theforge.config import ForgeConfig
from theforge.gate_diagnostics import run_gate_diagnostic_pass
from theforge.process_group import ProcessTeardown
from theforge.task import TaskStory
from theforge.validation_profiles import SelectedValidation, validation_run_record

from . import util as _cu
from .commit_guard import _commits_exist_strict, _has_commits_ahead_of_base
from .convention_baseline import resolve_convention_baseline_ref
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
from .run_setup import save_trajectory_state
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
    """Routing outcome of one VALIDATE run.

    ``RETRY_DEV`` hands the finding back to the dev inside the current review
    cycle, spending one dev iteration. ``RETRY_DEV_NEW_CYCLE`` does the same
    after the per-cycle dev pool is spent: the engine opens a review cycle,
    records the finding in ``state.validate_blocks``, and resets the dev budget
    so the finding has iterations to be fixed in (#1981).
    """

    PASS = auto()
    RETRY_DEV = auto()
    RETRY_DEV_NEW_CYCLE = auto()
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

    ``iter_num`` is ``state.dev_trace_count`` — the monotonic dev counter, not the
    per-cycle ``dev_iteration``. The two agreed until a review cycle could be
    opened from VALIDATE; after that the reset counter repeats iteration numbers
    and each new cycle's gate trace overwrote the previous cycle's, destroying
    the first failure's evidence on the repeated-failure path this fix creates
    (#1981). It also pairs the gate trace with the ``{n}-dev-output.txt`` of the
    same iteration.
    """
    if iter_num is None:
        return None
    return f".forge/traces/{iter_num}-gate.txt"


def _gate_output_digest(digest: list[str]) -> str | None:
    """Return the full-output digest ``run_gate_full`` produced, if it produced one."""
    return digest[0] if digest else None


def _gate_attempts(state: CoordinatorState) -> int:
    """Return the number of gate executions actually performed for this story.

    ``state.gate_runs`` is incremented once per gate command invocation —
    including invocations that timed out or errored, and excluding runs where
    ``gate_override`` skipped the gate entirely — and it is restored from the
    resume sidecar, so it counts the story's gate executions across resumes.
    ``state.gate_decisions`` cannot serve this role: it is appended only on the
    path that produced a decision, so timeouts and errors are missing from it,
    and the skip path appends a synthetic ``PASS`` for a gate that never ran
    (#1984). ``state.dev_iteration`` is the per-cycle dev counter: it also
    advances on iterations that never reached the gate (transport retries,
    max-iteration resumes), and it resets when a review cycle opens. Reporting
    it as an attempt count made a single un-retried gate failure read as
    exhaustion (#1981), so terminal messages count gate runs instead.
    """
    return state.gate_runs


def _persist_trajectory(state: CoordinatorState, workspace_path: Path, what: str) -> None:
    """Write the resume sidecar. A write failure degrades data, never the run."""
    try:
        save_trajectory_state(workspace_path, state)
    except Exception as exc:  # noqa: BLE001
        _log_verbose(f"Could not persist {what}: {exc}")


def _record_validation_run(
    state: CoordinatorState,
    *,
    selection: SelectedValidation | None,
    decision: str | None,
    commit: str | None,
    skipped: bool = False,
) -> None:
    """Append the provenance of one validation run (#2358).

    Written for every run the phase performs, authoritative or not: what a
    verdict is worth is a property of the profile behind it, and a record that
    omits the profile forces that question to be answered by reading a command
    string. ``selection`` is None only when no command ran at all (a suppressed
    gate), which is recorded as a skipped advisory run rather than a pass.
    """
    state.validation_runs.append(
        validation_run_record(selection, result=decision, commit=commit, skipped=skipped)
    )


def _record_gate_commit(
    state: CoordinatorState,
    workspace_path: Path,
    decision: str,
    *,
    selection: SelectedValidation | None = None,
    skipped: bool = False,
) -> None:
    """Record which commit the gate just judged, and what it decided (#2052).

    Stored as a pair so a review cycle can say whether the commit its reviewers
    read is the commit the gate ran on — a verdict against an earlier tree is
    not evidence about the current one. Recorded for the skip path too, with
    decision ``SKIPPED``, so an override reads as a suppressed gate rather than
    as a passing one. A HEAD read that fails leaves the commit unset, which
    renders as ``ungated``: unknown provenance, never assumed-current.

    Persisted here rather than only on the gate-run path. The skip path records
    a decision without a gate run, so hanging persistence off ``_record_gate_run``
    lost the pair whenever a story stopped after VALIDATE and resumed into
    REVIEW — the resumed cycle then rendered a deliberately suppressed gate as
    ``ungated``, which is exactly the wrong verification state to report.
    """
    ok, out = _cu._run_shell("git rev-parse HEAD", workspace_path)
    sha = out.strip() if ok else ""
    # A verdict is written only from a run that carries merge authority. A run
    # of any other profile — a story gate_override in a project that declares
    # profiles — is still recorded, as advisory, but it does not become the
    # story's gate decision, so nothing downstream can rest a merge on it
    # (#2358). The skip path passes no selection and keeps its historical
    # SKIPPED provenance, which reads as a suppressed gate rather than a pass.
    _authoritative = selection is None or selection.is_merge_authority
    if _authoritative:
        state.last_gate_decision = decision
        state.last_gate_commit = sha or None
    _record_validation_run(
        state, selection=selection, decision=decision, commit=sha or None, skipped=skipped
    )
    _persist_trajectory(state, workspace_path, "gate-commit provenance")


# Which validation-phase command a recorded teardown came from. Four different
# shells run in this phase and any of them can leave workers behind; a record
# that cannot say which one sends an operator to read the wrong trace (#2309).
VALIDATE_SHELL_GATE = "gate"
VALIDATE_SHELL_GATE_DEBUG = "gate_debug"
VALIDATE_SHELL_GATE_DIAGNOSTIC = "gate_diagnostic"
VALIDATE_SHELL_PRE_VALIDATE = "pre_validate"
# Not a validation shell at all: workspace setup runs before any gate, so its
# ``gate_run`` is legitimately 0. The source is what tells the two apart.
SHELL_WORKSPACE_SETUP = "workspace_setup"


def _record_gate_teardowns(
    state: CoordinatorState,
    teardowns: list[ProcessTeardown],
    *,
    source: str = VALIDATE_SHELL_GATE,
) -> None:
    """Record processes a validation shell left running, against the gate that ran.

    Drains the collector so the list can be reused by the shells that follow: the
    debug and diagnostic commands after a timeout, and the pre-validate command
    after a pass, are each a separate command that can leave its own workers
    behind, and none of them should re-record what an earlier shell accounted
    for.

    ``source`` names which of those commands leaked. Four different commands run
    through this phase, and a record that cannot tell them apart sends an
    operator to read the wrong trace.

    ``state.gate_runs`` must already be incremented — the ordinal here is the
    same one every other run-gated telemetry uses, and an off-by-one would put
    the first gate's leak under a gate number that never ran (#2309).
    """
    while teardowns:
        state.gate_process_teardowns.append(
            {
                "gate_run": state.gate_runs,
                "source": source,
                **teardowns.pop(0).to_audit_dict(),
            }
        )


def _record_gate_run(
    state: CoordinatorState,
    workspace_path: Path,
    decision: str | None = None,
    *,
    selection: SelectedValidation | None = None,
) -> None:
    """Count one gate execution and persist the count for ``--resume``.

    Called immediately after the gate command returns, before any routing, so
    the run is counted whether it produced a decision, timed out, or errored.
    Persisting here (rather than only at review-cycle classification) is what
    lets a story that escalates straight out of VALIDATE carry its full count
    into the next resumed run (#1984). A sidecar write failure must not fail
    the run: the count degrades, the story does not.
    """
    state.gate_runs += 1
    if decision is not None:
        # Writes the whole sidecar, incremented gate_runs included.
        _record_gate_commit(state, workspace_path, decision, selection=selection)
    else:
        _persist_trajectory(state, workspace_path, "gate-run count")


def _new_review_cycle_available(state: CoordinatorState, config: ForgeConfig) -> bool:
    """Return whether another review cycle may be opened for this story.

    Mirrors the engine's post-increment ``review_cycle >= cap`` guard, which
    stays in place as the loop bound.
    """
    cap = state.adaptive_review_max or config.retry.max_review_cycles
    return state.review_cycle + 1 < cap


class _BlockRoute(NamedTuple):
    """Where a coordinator-observed blocking finding goes, and why.

    ``reason`` is carried rather than re-derived at each escalation site so the
    operator message, the audit flag, and the routing decision cannot drift out
    of agreement about which budget or signal stopped the retry.
    """

    outcome: _ValidateOutcome
    reason: str


_BLOCK_REASONS = {
    "dev_budget_remains": "dev iterations remain in this review cycle",
    "review_cycle_bought": "the dev pool is spent; a review cycle was opened for the finding",
    "p2_cleanup": (
        "P2 cleanup spends the existing dev iteration pool and never opens a review cycle"
    ),
    "gate_signature_stalled": (
        "the gate produced identical output on the last two iterations, so another"
        " review cycle would not change it"
    ),
    "budgets_exhausted": "dev iterations and review cycles are both exhausted",
}


def _blocking_finding_route(state: CoordinatorState, config: ForgeConfig) -> _BlockRoute:
    """Route a coordinator-observed blocking finding (gate failure or hard convention).

    Gate execution is coordinator-owned (#1948), so the dev never sees a gate
    result unless VALIDATE hands it back. The finding is charged to the dev
    iteration pool first. Once that pool is spent it is charged to a review
    cycle — the same currency ``review_phase`` spends when a reviewer requests
    changes, and for the same reason: a coordinator-observed blocking finding
    *is* a REQUEST_CHANGES, raised by the coordinator rather than a reviewer.
    Terminal only when both budgets are gone, or when the failure has stopped
    moving and another cycle would buy nothing.
    """
    if not state.budget.is_exhausted():
        return _BlockRoute(_ValidateOutcome.RETRY_DEV, "dev_budget_remains")
    # P2 cleanup runs after APPROVE and is deliberately capped by the existing
    # dev pool (engine skips reset_cycle for it), so it never buys a new cycle:
    # a cleanup iteration that breaks the gate stays terminal.
    if state.p2_cleanup_active:
        return _BlockRoute(_ValidateOutcome.ESCALATE, "p2_cleanup")
    # Buying a cycle is only worth it if the failure is still moving. An
    # unchanged gate signature across the last two iterations means the dev is
    # not converging, and another pool of iterations would cost the full
    # dev × cycle cross-product to learn the same thing.
    if _gate_signature_stalled(state):
        return _BlockRoute(_ValidateOutcome.ESCALATE, "gate_signature_stalled")
    if _new_review_cycle_available(state, config):
        return _BlockRoute(_ValidateOutcome.RETRY_DEV_NEW_CYCLE, "review_cycle_bought")
    return _BlockRoute(_ValidateOutcome.ESCALATE, "budgets_exhausted")


def _gate_signature_stalled(state: CoordinatorState) -> bool:
    """Return True when the last two iterations produced identical gate output.

    ``_is_identical_failure`` needs named failing tests, so it cannot see a
    lint- or format-only failure — the #1972 shape, and exactly the class this
    fix makes cheap to retry. Comparing output fingerprints catches it without
    parsing any toolchain's format.

    Deliberately narrow: it gates only the *purchase* of a new review cycle, so
    a dev always gets its full in-cycle iterations first, and it needs two
    recorded iterations with equal non-null fingerprints — output carrying a
    duration or any varying token never matches, which lets the retry proceed.
    """
    telemetry = state.dev_iteration_telemetry
    if len(telemetry) < 2:
        return False
    curr = telemetry[-1].gate_output_fingerprint
    prev = telemetry[-2].gate_output_fingerprint
    return curr is not None and curr == prev


def record_validate_block(state: CoordinatorState, *, outcome: str, reason: str) -> None:
    """Record a coordinator-raised blocking finding in VALIDATE's own channel.

    These findings are deliberately NOT written into ``review_results`` /
    ``review_cycle_metadata`` / ``review_iteration_telemetry``. Those three lists
    are the *reviewer* record: an entry in them means a reviewer pool ran, and
    per-model findings/cost attribution feeding ``model_profiles.yaml``, the
    adaptive review-cycle learner, and the persistent-P1 lookback at
    ``review_results[-2]`` all depend on that (#1981).

    ``outcome`` is ``opened_review_cycle`` or ``terminal``; ``reason`` is the
    routing key from ``_blocking_finding_route``, so the audit carries the
    coordinator's decision structurally rather than only as prose in an error
    string.
    """
    kind = "convention" if state.retry_reason == RetryReason.CONVENTION_VIOLATIONS else "gate"
    fingerprint = (
        state.dev_iteration_telemetry[-1].gate_output_fingerprint
        if state.dev_iteration_telemetry
        else None
    )
    state.validate_blocks.append(
        {
            "kind": kind,
            "outcome": outcome,
            "reason": reason,
            "review_cycle": state.review_cycle,
            "dev_iterations_spent": state.budget.cycle_count,
            "gate_decision": state.gate_decisions[-1] if state.gate_decisions else None,
            "gate_output_fingerprint": fingerprint,
            "detail": state.validate_block_detail or "",
            "convention_violations": (
                list(state.convention_violations) if kind == "convention" else []
            ),
        }
    )


def _apply_block_route(state: CoordinatorState, route: _BlockRoute) -> str:
    """Record what a terminal routing decision means for budget reporting.

    ``review_budget_exhausted`` is the fact the router acted on: no further
    review cycle could be opened. Usage reporting reads it rather than comparing
    counters, because a story stopped by that condition has a cycle in flight and
    would otherwise be reported as having finished early with budget to spare.
    """
    if route.reason == "budgets_exhausted":
        state.review_budget_exhausted = True
    if route.outcome is _ValidateOutcome.ESCALATE:
        record_validate_block(state, outcome="terminal", reason=route.reason)
    return _BLOCK_REASONS[route.reason]


def _route_suffix(route: _ValidateOutcome) -> str:
    """Return the operator-facing suffix describing which budget a retry spends."""
    if route is _ValidateOutcome.RETRY_DEV_NEW_CYCLE:
        return ", in a new review cycle"
    return ""


def _test_file_exists_in_head(workspace_path: Path, test_file: str) -> bool:
    """Return whether the failing test file exists in the current checkout."""
    return workspace_path.joinpath(test_file).is_file()


_UNRECOGNIZED_GATE_FORMAT_NOTE = (
    "\n\nNo failing-test identifiers could be extracted from this gate's output:"
    " its format is not recognized by core's built-in test-runner extractor and no"
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
    # Which commit supplied the ceilings decides what this story is answerable
    # for, so record it rather than leaving it to be reconstructed (ADR-0008).
    _log_verbose(
        f"  Convention baseline: {baseline_ref or 'none — configured limit is the ceiling'}"
    )
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
    else:
        result = _cu._run_worktree_eval(
            workspace_path,
            "check_conventions",
            {"config": _config_dict, "project_root": str(workspace_path)},
        )
    # Both branches answer the same two questions: "all_violations" is the plain
    # scan the advisory artifact reads, "violations" is what blocks.
    all_v = [types.SimpleNamespace(**d) for d in result["all_violations"]]
    net_v = [types.SimpleNamespace(**d) for d in result["violations"]]
    return all_v, net_v


def _record_advisory_convention_state(
    config: ForgeConfig,
    task: TaskStory,
    state: CoordinatorState,
    violations: list[dict],
    logger: StructuredLogger | None,
) -> None:
    """Persist rolling advisory convention state for the current scan.

    A failure to persist the artifact is a failure of infrastructure every story
    in the sprint shares, not of this story's work (#2107). It is recorded
    against the run's shared-infrastructure ledger and surfaced in the audit,
    and the story keeps its real phase, outcome, and cost accounting — the
    advisory artifact is non-blocking by definition, so losing one update must
    never cost a completed story its result.
    """
    try:
        advisory_result = update_advisory_violations(
            config,
            violations,
            observed_at=dt.datetime.now(dt.timezone.utc),
            run_id=state.run_id,
            story_slug=task.slug,
        )
    except AdvisoryArtifactError as exc:
        failure = exc.as_failure_record()
        state.shared_infrastructure_failures.append(failure)
        _log(f"  ⚠ Advisory artifact persistence failed (shared infrastructure): {exc}")
        if logger:
            logger._safe_emit("convention_advisory_persist_failed", **failure)
        return
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
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                # Carry the story-descriptive complexity fields on VALIDATE entry
                # so this early payload agrees with the sibling VALIDATE payload
                # (post-gate) and every other phase. Omitting them here left the
                # live display inconsistent across phases (issue #1921).
                **_cu.live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
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
    # SHA-256 of the full gate output, supplied by run_gate_full. Empty when the
    # gate was skipped or run_gate_full was stubbed; the stall brake then has no
    # signature to compare and fails open.
    _gate_digest: list[str] = []
    # Populated only when the gate command left processes running that teardown
    # had to kill (#2309). `make gate` runs the project's test runner, which is
    # exactly the shape that spawns workers outliving the run that started them,
    # and a leaked worker produces no artifact of its own — so the record has to
    # come from here or not at all.
    _gate_teardowns: list[ProcessTeardown] = []
    # Which validation profile the gate actually ran, filled in by run_gate_full.
    # VALIDATE always asks for the merge-authority profile; the selection comes
    # back so the recorded result can say what it is worth (#2358).
    _gate_selection: list[SelectedValidation] = []
    if _is_gate_skip(gate_override):
        _log_phase(state.phase, "skipped (gate: none)")
        _log("  Gate: none (story override)")
        gate_decision: str | None = "PASS"
        gate_err: str | None = None
        gate_result_for_telemetry = gate_decision
        # No gate ran, so this is not a gate run — but the commit still has a
        # verification state, and "suppressed by override" is that state.
        _record_gate_commit(state, workspace_path, "SKIPPED", skipped=True)
    else:
        if gate_override is not None:
            _log_phase(state.phase, "running gate... (override)")
            _log(f"  Gate: {gate_override} (story override)")
        else:
            _log_phase(state.phase, "running gate...")
        gate_decision, gate_err, gate_output_tail, resolved_gate_cmd, gate_exit_code = (
            run_gate_full(
                config,
                workspace_path,
                task=task,
                iter_num=state.dev_trace_count,
                output_digest=_gate_digest,
                process_teardowns=_gate_teardowns,
                selection_out=_gate_selection,
            )
        )
        # The gate command ran. Count it here — before decision/error routing —
        # so timeouts and errors, which return without ever appending to
        # gate_decisions, are still counted as the executions they were (#1984).
        _record_gate_run(
            state,
            workspace_path,
            decision=gate_decision or "ERROR",
            selection=_gate_selection[0] if _gate_selection else None,
        )
        # After the counter, so a leak from the first gate is tagged gate_run 1
        # like every other run-gated telemetry rather than 0 (#2309).
        _record_gate_teardowns(state, _gate_teardowns)
        # ── Widen an advisory run to the merge-authority profile ──────────
        # A story gate_override under declared profiles runs an undeclared
        # command, whose result is advisory. A PASS from it is not a verdict,
        # and letting VALIDATE return PASS on it would carry the story to
        # REVIEW and DONE with no merge-authority result behind it. So the
        # advisory run widens: the declared merge-authority profile runs too,
        # in the same worktree, and *its* result is the verdict. Unknown input
        # causes more validation to run, never less (#2358).
        #
        # Only a passing advisory run widens. A failing one already blocks
        # progression — advisory results may inform routing, they just cannot
        # establish trust — so paying for the complete profile after it would
        # buy no decision that is not already made.
        _advisory_selection = _gate_selection[0] if _gate_selection else None
        if (
            _advisory_selection is not None
            and not _advisory_selection.is_merge_authority
            and gate_decision == "PASS"
            and gate_err is None
        ):
            _log(
                f"  Gate override passed but is advisory ({_advisory_selection.describe()}); "
                "widening to the merge-authority profile for the verdict"
            )
            # Both out-parameters are reset first: everything downstream — the
            # stall brake's output signature, the recorded selection, the
            # gate_decisions append — must describe the run that produced the
            # verdict, not the advisory run that preceded it.
            _gate_selection.clear()
            _gate_digest.clear()
            gate_decision, gate_err, gate_output_tail, resolved_gate_cmd, gate_exit_code = (
                run_gate_full(
                    config,
                    workspace_path,
                    task=task,
                    iter_num=state.dev_trace_count,
                    output_digest=_gate_digest,
                    process_teardowns=_gate_teardowns,
                    selection_out=_gate_selection,
                    ignore_gate_override=True,
                )
            )
            _record_gate_run(
                state,
                workspace_path,
                decision=gate_decision or "ERROR",
                selection=_gate_selection[0] if _gate_selection else None,
            )
            _record_gate_teardowns(state, _gate_teardowns)
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
                iter_num=state.dev_trace_count,
                process_teardowns=_gate_teardowns,
            )
            _record_gate_teardowns(state, _gate_teardowns, source=VALIDATE_SHELL_GATE_DEBUG)
            if debug_telemetry is not None:
                state.gate_debug_telemetry.append(debug_telemetry)
                gate_err = (
                    f"{gate_err}. Gate debug command ran; see audit "
                    f"iterations.gate_debug[-1] (trace_index "
                    f"{debug_telemetry.trace_index}) and trace "
                    f"{debug_telemetry.trace_path}."
                    f"\nGate debug output tail:\n{debug_telemetry.output_tail}"
                )
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result=gate_result_for_telemetry,
            gate_output_tail=gate_output_tail or gate_err,
            gate_output_digest=_gate_output_digest(_gate_digest),
            is_timeout=is_timeout,
            failed_test_pattern=config.validation.failed_test_pattern,
        )
        # Timeout with dev commits is a retryable validation failure: hand back
        # to dev with a timeout-RCA evidence packet rather than escalating
        # terminally. Routes only when (a) the gate actually timed out, (b) the
        # dev iteration produced commits ahead of base, (c) budget remains
        # (dev iterations, or a review cycle once those are spent), and (d) the
        # failure is not a consecutive-identical timeout (circuit breaker still
        # owns that case). Infrastructure errors (state 4) and empty-worktree
        # timeouts (state 1) continue to escalate.
        _timeout_route = _blocking_finding_route(state, config)
        # Split the shape of the failure from the budget available to it: a
        # retryable-shaped timeout that only the budget stopped must record why,
        # so a terminal budget refusal is not reported as a story that finished
        # early with cycles to spare (#1981).
        _timeout_retryable = (
            is_timeout
            and not _is_identical_failure(state.dev_iteration_telemetry)
            and _commits_exist_strict(workspace_path, config.workspace.base_branch)
        )
        if _timeout_retryable and _timeout_route.outcome is not _ValidateOutcome.ESCALATE:
            # The original gate process group was already killed by
            # _run_shell_detailed on timeout (spec step 1). Run the diagnostic
            # re-run pass in the same worktree before constructing the retry
            # input so the dev agent gets the hanging test + stack trace on the
            # first retry rather than having to re-run the suite manually.
            diagnostic = run_gate_diagnostic_pass(
                config,
                workspace_path,
                task=task,
                iter_num=state.dev_trace_count,
                process_teardowns=_gate_teardowns,
            )
            _record_gate_teardowns(state, _gate_teardowns, source=VALIDATE_SHELL_GATE_DIAGNOSTIC)
            if diagnostic is not None:
                state.gate_diagnostic_telemetry.append(diagnostic)
                if logger:
                    logger._safe_emit(
                        "gate_diagnostic",
                        trace_index=diagnostic.trace_index,
                        trace_path=diagnostic.trace_path,
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
            state.validate_block_detail = gate_err
            _log(
                f"  ✗ VALIDATE   TIMEOUT  (iter={state.dev_iteration}"
                f" → retrying dev with RCA packet{_route_suffix(_timeout_route.outcome)})"
            )
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="fail")
            return _timeout_route.outcome, None
        # Check consecutive identical failures (including timeouts) before escalating.
        if _is_identical_failure(state.dev_iteration_telemetry):
            state.phase = Phase.ESCALATE
            state.error = (
                f"Identical gate failure on consecutive iterations"
                f" (iteration {state.dev_iteration}): gate error: {gate_err}."
                f" Remaining retry budget: {state.budget.remaining()}."
            )
        elif _timeout_retryable:
            # Retryable in shape; the budget is what stopped it. Record the
            # refusal so usage reporting can tell this from an early finish.
            state.phase = Phase.ESCALATE
            state.error = f"{gate_err}; {_apply_block_route(state, _timeout_route)}"
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
    # Appended only for a run that carries merge authority (or the legacy path,
    # where the selection is the gate command itself). An advisory run's result
    # is already recorded in ``state.validation_runs``; letting it into the
    # decision history would make it indistinguishable from a verdict (#2358).
    _recorded_selection = _gate_selection[0] if _gate_selection else None
    if _recorded_selection is None or _recorded_selection.is_merge_authority:
        state.gate_decisions.append(gate_decision)
    else:
        _log(
            f"  Gate result recorded as advisory ({_recorded_selection.describe()}): "
            "it does not establish merge authority."
        )
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "VALIDATE",
                "iteration": state.dev_iteration,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                **_cu.live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "detail": {"gate_status": gate_decision},
            }
        )
    _log_verbose(f"Gate decision: {gate_decision}")

    if gate_decision == "PASS":
        _log("  ✓ VALIDATE   PASS")
        pre_validate_cmd = config.validation.pre_validate_command
        if pre_validate_cmd:
            _log(f"  Running pre-validate command: {pre_validate_cmd}")
            pv_ok, pv_out = _cu._run_shell(
                pre_validate_cmd, workspace_path, teardown_out=_gate_teardowns
            )
            # Recorded against the gate that has just passed, and named so it is
            # not read as that gate's own leak: this is a project command
            # configured to run after the gate, and it spawns whatever it likes.
            _record_gate_teardowns(state, _gate_teardowns, source=VALIDATE_SHELL_PRE_VALIDATE)
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
                        "gate output does not match core's built-in test-runner"
                        " grammar and no validation.failed_test_pattern is configured"
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
        state.validate_block_detail = format_gate_failure_summary(
            f"Gate returned {gate_decision}",
            exit_code=gate_exit_code,
            output_tail=gate_output_tail,
            tail_chars=config.validation.gate_output_tail_chars,
            trace_path=_gate_trace_path(state.dev_trace_count),
        )
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result=gate_decision,
            gate_output_tail=gate_output_tail,
            gate_output_digest=_gate_output_digest(_gate_digest),
            failed_test_pattern=config.validation.failed_test_pattern,
        )
        if state.dev_iteration_telemetry:
            state.dev_iteration_telemetry[-1] = dataclasses.replace(
                state.dev_iteration_telemetry[-1],
                existing_test_failures=existing_test_failures,
            )
        # A gate failure is terminal only once the dev iteration pool AND the
        # review-cycle pool are spent; while either has room the finding goes
        # back to the dev that can fix it (#1981).
        _gate_route = _blocking_finding_route(state, config)
        _gate_route_reason = _apply_block_route(state, _gate_route)
        if _gate_route.outcome is _ValidateOutcome.ESCALATE:
            state.phase = Phase.ESCALATE
            state.error = format_gate_failure_summary(
                f"Gate returned {gate_decision} after {_gate_attempts(state)} gate run(s);"
                f" {_gate_route_reason}",
                exit_code=gate_exit_code,
                output_tail=gate_output_tail,
                tail_chars=config.validation.gate_output_tail_chars,
                trace_path=_gate_trace_path(state.dev_trace_count),
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        _log(
            f"  ✗ VALIDATE   {gate_decision}  (iter={state.dev_iteration}"
            f" → retrying{_route_suffix(_gate_route.outcome)})"
        )
        _log(f"Retrying dev (gate={gate_decision}, iter={state.dev_iteration})")
        if _is_identical_failure(state.dev_iteration_telemetry):
            state.phase = Phase.ESCALATE
            state.error = format_gate_failure_summary(
                f"Identical gate failure on consecutive iterations"
                f" (iteration {state.dev_iteration}): gate returned {gate_decision}."
                f" Remaining retry budget: {state.budget.remaining()}.",
                exit_code=gate_exit_code,
                output_tail=gate_output_tail,
                tail_chars=config.validation.gate_output_tail_chars,
                trace_path=_gate_trace_path(state.dev_trace_count),
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
        return _gate_route.outcome, None
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
                state.validate_block_detail = human_feedback
                # Same routing as a gate failure: a hard convention violation is
                # a mechanical, dev-fixable finding, so it spends dev iterations
                # first and only buys a review cycle once those are gone (#1981).
                _cv_route = _blocking_finding_route(state, config)
                _cv_route_reason = _apply_block_route(state, _cv_route)
                if _cv_route.outcome is _ValidateOutcome.ESCALATE:
                    state.phase = Phase.ESCALATE
                    state.error = (
                        "Hard convention violations after"
                        f" {_gate_attempts(state)} validation run(s);"
                        f" {_cv_route_reason}"
                    )
                    _log(f"✗ ESCALATE   {state.error}")
                    if logger:
                        logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                        logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
                    _escalate_notify(task, state, notify, config)
                    return _ValidateOutcome.ESCALATE, CoordinatorResult(
                        success=False, phase=state.phase, state=state, message=state.error
                    )
                _log(f"  ↩ VALIDATE   returning to dev{_route_suffix(_cv_route.outcome)}")
                if logger:
                    logger._safe_emit("phase_end", phase="VALIDATE", outcome="convention_fail")
                return _cv_route.outcome, None
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
    return resolve_convention_baseline_ref(workspace_path, base_branch)
