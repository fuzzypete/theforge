"""DEV phase handler: prompt routing, agent invocation, budget enforcement, zero-change guard."""

from __future__ import annotations

import functools
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace as _dc_replace
from pathlib import Path

import yaml

from theforge import worker_budget as _worker_budget
from theforge.agent_types import FAILURE_ENDED_WITHOUT_RESULT
from theforge.config import ForgeConfig, apply_model_info
from theforge.config.auth import sandbox_available_for_profile, sandbox_containment_mode
from theforge.config.sandbox_capabilities import SandboxCapabilityError, resolve_capabilities
from theforge.config.types import StuckDetectionConfig
from theforge.coordinator.context_scope import plan_file_list
from theforge.review import append_convention_retry_findings
from theforge.schemas import dev_handoff_claims_unproven_completion
from theforge.sessions import save_sessions
from theforge.task import (
    ContextAssembler,
    TaskStory,
    build_batch_dev_prompt,
    build_dev_prompt,
    build_fix_prompt,
    render_batch_spec_section,
    render_resolved_spec_gaps_section,
    render_spec_gap_section,
    render_verification_section,
)
from theforge.traces import write_trace
from theforge.validation_profiles import PHASE_ADVISORY, merge_profile, select_validation

from .agent_failure import (
    CATEGORY_TRANSPORT,
    AgentInvocationFailure,
    carries_agent_text,
    classify_agent_failure,
    mark_infrastructure_abort,
    produced_model_output,
    record_invocation_failure,
    zero_charge_no_model_artifacts,
)
from .commit_guard import (
    _checkpoint_commit,
    _commits_exist_strict,
    _has_commits_ahead_of_base,
    _worktree_changed_since_commit,
    _worktree_has_changes,
)
from .dev_verification import DevVerificationBroker
from .gate import _is_gate_skip
from .logging import StructuredLogger
from .notify import _escalate_notify
from .preflight import _escalate_dev_model, _find_registry_key_for_profile
from .spec_gap_flow import handle_spec_gap, resolved_gaps_for_prompt, spec_gap_pauses_remaining
from .state import (
    CoordinatorResult,
    CoordinatorState,
    DevIterationTelemetry,
    FailedTestExtraction,
    Phase,
    RetryReason,
)
from .util import (
    _fmt_cost,
    _fmt_duration,
    _log,
    _log_phase,
    _log_verbose,
    cap_timeout_to_story_ceiling,
    clamp_timeout_to_remaining,
    resolve_timeout_with_active,
    sum_costs,
)
from .worktree_state import check_worktree_git_consistency

# ── Lazy runner slot ──────────────────────────────────────────────────
# None until first call; tests may replace before calling run_task.
# Patch targets:
#   theforge.coordinator.dev_phase.run_agent        — dev agent call
#   theforge.coordinator.dev_phase.log_agent_result — dev result logging
run_agent = None
log_agent_result = None

_RUNNER_FAILURE_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "runner_argument_error",
        (
            "error: unexpected argument",
            "unexpected argument",
            "unrecognized option",
            "unknown option",
            "invalid option",
        ),
    ),
    (
        "runner_command_not_found",
        (
            "command not found",
            "no such file or directory",
        ),
    ),
    (
        "runner_permission_denied",
        ("permission denied",),
    ),
)
_SHELL_ERROR_PREFIXES = ("bash:", "sh:", "zsh:")
_DEV_TRANSPORT_RETRY_BACKOFF_BASE_SECONDS = 2

# How much captured agent text a failure message quotes. Long enough to carry
# the statement an agent ended on, short enough that the message stays usable as
# a log line, a checkpoint commit subject, and the audit's outcome.message.
_AGENT_TEXT_MAX_CHARS = 400

# Forge-emitted phrase naming an ``agent_ended_without_result`` ending (#2427).
# Sprint RCA matches it when a run's per-iteration telemetry is unavailable —
# keep it in sync with ``sprint.rca._ENDED_WITHOUT_RESULT_PHRASE``.
ENDED_WITHOUT_RESULT_PHRASE = (
    "stopped producing output and its process ended without a result event"
)

# Marker introducing the captured agent text inside that message. RCA reads it
# to quote the agent's words rather than the sentence wrapped around them —
# keep it in sync with ``sprint.rca._LAST_SAID_MARKER``.
LAST_SAID_MARKER = "it last said: "

_TRANSIENT_DEV_ERROR_PATTERNS = (
    "rate limit",
    "rate-limited",
    "resource_exhausted",
    "resource exhausted",
    "quota exceeded",
    "quota_exceeded",
    "internal error",
    "server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "stream idle timeout",
    "partial response received",
    "mid-stream disconnect",
    "stream disconnected",
    "connection reset",
    "connection-reset",
    "econnreset",
    "connection aborted",
    "peer closed connection",
    "temporarily unavailable",
    "try again later",
    "timeout awaiting headers",
)
# HTTP status codes that indicate a transient provider failure. Matched with
# digit-boundary anchoring so they don't fire on substrings of unrelated
# numbers (port numbers, token counts, etc.) that happen to contain these
# digits, e.g. "5003" or "1500". "connection refused" is deliberately not
# treated as transient: it signals a misconfigured or unreachable endpoint,
# not transient load, so retrying is unlikely to succeed.
_TRANSIENT_DEV_ERROR_STATUS_CODES = ("429", "500", "502", "503", "504")
_TRANSIENT_DEV_ERROR_STATUS_CODE_RE = re.compile(
    r"(?<!\d)(?:" + "|".join(_TRANSIENT_DEV_ERROR_STATUS_CODES) + r")(?!\d)"
)


def _scale_stuck_for_complexity(
    cfg: StuckDetectionConfig,
    complexity: str | None,
    plan_file_count: int,
) -> StuckDetectionConfig:
    """Return a StuckDetectionConfig with thresholds scaled by complexity and plan size.

    LARGE/medium stories legitimately need more pre-modification exploration; flat
    thresholds false-terminate competent dev agents. Scaling raises (never lowers)
    no_progress_iterations and post_nudge_iterations:
      - no_progress_iterations: base × multiplier(complexity), plus plan_file_count
        so a plan touching many files gives the agent room to read each one.
      - post_nudge_iterations: base × multiplier(complexity), giving complex stories
        a meaningful grace window after the nudge.
    """
    np_mult = cfg.no_progress_multipliers.get(complexity or "", 1.0) if complexity else 1.0
    pn_mult = cfg.post_nudge_multipliers.get(complexity or "", 1.0) if complexity else 1.0
    scaled_no_progress = max(
        cfg.no_progress_iterations,
        round(cfg.no_progress_iterations * np_mult) + max(plan_file_count, 0),
    )
    scaled_post_nudge = max(
        cfg.post_nudge_iterations,
        round(cfg.post_nudge_iterations * pn_mult),
    )
    return _dc_replace(
        cfg,
        no_progress_iterations=scaled_no_progress,
        post_nudge_iterations=scaled_post_nudge,
    )


def _plan_files_for_stuck_scaling(
    state: CoordinatorState,
    logger: StructuredLogger | None,
) -> list[str]:
    """Return the plan's target files for stuck-detection scaling.

    A plan must populate ``state.plan_structured`` before DEV entry (non-resume:
    plan_flow after the PLAN agent; resume: run_setup.load_plan_state from the
    worktree's .forge/plan.md). When it is ``None`` here, the plan structure
    never reached the dev phase, so the +N plan-scope exploration bonus silently
    collapses to zero (issue #1135). Surface that as a structured warning naming
    the missing field rather than letting a bare 0 flow into policy — a genuinely
    file-less plan yields a non-None structure with an empty file list, which is
    distinct from this degraded case and does not warn.
    """
    if state.plan_structured is None:
        _log_verbose(
            "  ⚠ DEV   plan_structured missing from state — stuck-detection scaling "
            "proceeds with 0 plan files (no plan-scope exploration bonus)"
        )
        if logger:
            logger._safe_emit(
                "plan_structured_missing",
                phase="DEV",
                iteration=state.dev_iteration,
                field="plan_structured",
                consumer="stuck_detection_scaling",
            )
        return []
    return plan_file_list(state.plan_structured)


def _summarize_runner_failure(output: str, indicators: tuple[str, ...]) -> str:
    """Return a short, operator-friendly summary line for a runner crash."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    lowered = tuple(line.lower() for line in lines)
    for indicator in indicators:
        for idx, line in enumerate(lowered):
            if indicator in line:
                return lines[idx][:200]
    for idx, line in enumerate(lowered):
        if not line.startswith("usage:"):
            return lines[idx][:200]
    return lines[0][:200] if lines else "(no output)"


def _runner_failure_evidence(output: str, exit_code: int) -> list[str]:
    """Return candidate shell-level crash lines from runner output."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return []
    candidates = lines[:3]
    if len(lines) > 3:
        candidates.extend(lines[-2:])
    lowered = [line.lower() for line in candidates]
    if exit_code == 127:
        return [
            line
            for line, lowered_line in zip(candidates, lowered, strict=False)
            if lowered_line.startswith(_SHELL_ERROR_PREFIXES)
            and "command not found" in lowered_line
        ]
    if exit_code == 126:
        return [
            line
            for line, lowered_line in zip(candidates, lowered, strict=False)
            if lowered_line.startswith(_SHELL_ERROR_PREFIXES)
            and "permission denied" in lowered_line
        ]
    return candidates


def classify_runner_subprocess_failure(output: str, exit_code: int) -> tuple[str, str] | None:
    """Classify a runner subprocess crash that occurred before agent execution."""
    evidence_lines = _runner_failure_evidence(output, exit_code)
    evidence_text = "\n".join(evidence_lines)
    lowered = evidence_text.lower()
    for failure_code, indicators in _RUNNER_FAILURE_SIGNATURES:
        if failure_code == "runner_command_not_found" and exit_code != 127:
            continue
        if failure_code == "runner_permission_denied" and exit_code != 126:
            continue
        if failure_code in {"runner_command_not_found", "runner_permission_denied"}:
            if not evidence_lines:
                continue
        if any(indicator in lowered for indicator in indicators):
            return failure_code, _summarize_runner_failure(evidence_text, indicators)
    return None


def _runner_display_name(config: ForgeConfig) -> str:
    """Return a stable operator-facing runner label for escalation messages."""
    return config.dev_profile.cli or config.dev_profile.provider or config.dev_profile.name


def _unrecoverable_provider_quota(result: object) -> str | None:
    """Explain a dev failure that is certain to repeat, or return None (#2298).

    Certainty needs all three facts together: the provider refused on quota, it
    named the moment that quota resets, and no configured transport fallback was
    applicable. A quota refusal without a stated reset time may clear on its own,
    so repeating it is reasonable; one that names its reset time will not, and
    spending the remaining iteration budget re-asking is a choice the run should
    not make on its own.
    """
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(result)}")
    if result.success or result.transport_fallback_fired:
        return None
    if not result.cli_quota_error_observed:
        return None
    if not result.provider_quota_reset_at:
        return None
    if not result.transport_fallback_not_applied_reason:
        return None
    return (
        f"provider quota exhausted until {result.provider_quota_reset_at}; "
        f"{result.transport_fallback_not_applied_reason}"
    )


def _is_transient_dev_failure(
    result: object, runner_failure: tuple[str, str] | None = None
) -> bool:
    """Return True when a failed dev invocation looks transient and retryable."""
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(result)}")
    if result.success or result.startup_failure or runner_failure is not None:
        return False
    # A quota refusal with a stated reset time and no applicable fallback is the
    # one provider failure that is known not to be transient — retrying it before
    # the stated time reproduces it by construction (#2298).
    if _unrecoverable_provider_quota(result) is not None:
        return False
    failure_code = (result.failure_code or "").lower()
    if failure_code in {"rate_limit", "provider_internal_error", "connection_reset"}:
        return True
    output = (result.output or "").lower()
    if _TRANSIENT_DEV_ERROR_STATUS_CODE_RE.search(output):
        return True
    return any(pattern in output for pattern in _TRANSIENT_DEV_ERROR_PATTERNS)


def _summarize_dev_transport_failure(result: object) -> str:
    """Produce a compact summary for audit/logging when dev transport fails."""
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(result)}")
    parts = [f"exit={result.exit_code}"]
    if result.failure_code:
        parts.append(f"failure_code={result.failure_code}")
    output = " ".join((result.output or "").split())
    if output:
        parts.append(output[:200])
    return ": ".join((parts[0], " | ".join(parts[1:]))) if len(parts) > 1 else parts[0]


def _clip_agent_text(output: str | None) -> str:
    """One-line, length-capped rendering of runner output for a failure message."""
    text = " ".join(str(output or "").split())
    if len(text) > _AGENT_TEXT_MAX_CHARS:
        text = text[:_AGENT_TEXT_MAX_CHARS].rstrip() + "…"
    return text


def _captured_agent_text(result: object) -> str | None:
    """Return the agent text a failed invocation left behind, or ``None`` (#2427).

    Only real agent words: a runner marker standing in for output that never
    existed (``TIMEOUT: ...``, ``CLAUDE_STREAM_NO_TEXT: ...``) is a statement by
    the runner about the process, and recording one as what the agent said would
    describe a silent run as having spoken.
    """
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(result, _AgentResult) or result.success:
        return None
    if not carries_agent_text(result.output):
        return None
    return _clip_agent_text(result.output)


def _describe_dev_failure(result: object, *, is_timeout: bool) -> str:
    """Name a failed dev invocation the way the run already recorded it (#2427).

    The exit status records what was done to the process, not what went wrong,
    so it is the last resort rather than the default. Two failures arrive
    already explained and are reported in those words:

    * a timeout, whose runner output states the limit that was exceeded;
    * an agent that ended without a result event, even when the stream captured
      no agent text and the run only knows the failure code.

    An ending the run cannot account for still reads ``exit=<code>`` — the
    distinction between "the run stated why it ended" and "the run has no
    recorded cause" is exactly what a caller downstream needs to keep.
    """
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(result)}")
    exit_detail = f"exit={result.exit_code}"
    if is_timeout:
        # The runner's own words state the limit that was exceeded. They are a
        # marker rather than agent text, and they are still the explanation.
        return _clip_agent_text(result.output) or exit_detail
    captured = _captured_agent_text(result)
    if result.failure_code == FAILURE_ENDED_WITHOUT_RESULT:
        if not captured:
            return f"{exit_detail}: the agent {ENDED_WITHOUT_RESULT_PHRASE}"
        return (
            f"{exit_detail}: the agent {ENDED_WITHOUT_RESULT_PHRASE}; {LAST_SAID_MARKER}{captured}"
        )
    return exit_detail


def _append_retry_guidance(existing_feedback: str | None, guidance: str) -> str:
    """Preserve prior feedback while appending a retry note only once."""
    if not existing_feedback:
        return guidance
    appended_block = f"Additional retry guidance:\n{guidance}"
    if existing_feedback.endswith(appended_block):
        return existing_feedback
    return f"{existing_feedback}\n\n{appended_block}"


def _dev_transport_retry_backoff_seconds(retry_count: int) -> int:
    """Return the backoff delay before the next transient dev retry."""
    return _DEV_TRANSPORT_RETRY_BACKOFF_BASE_SECONDS * (2 ** max(retry_count - 1, 0))


# Worker-prefix strip for parallel test-runner output, e.g. a leading "[gw7] "
# emitted by pytest-xdist workers. Kept as configuration-neutral regex text; the
# stack-specific origin is described here in a comment only.
_WORKER_PREFIX_RE = re.compile(r"^\[gw\d+\]\s+")
# Summary-line markers the built-in reader recognizes even when nothing failed
# (e.g. a gate whose test run passed cleanly but then failed a downstream
# lint/format step). These let "test runner ran, nothing failed" be told apart
# from "output is not in the built-in grammar at all". The recognized grammar is
# the one emitted by the built-in Python test runner this orchestrator ships with.
_BUILTIN_SUMMARY_RE = re.compile(
    r"\b\d+\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warnings?)\b",
    re.IGNORECASE,
)
_BUILTIN_COLLECTED_RE = re.compile(r"\bcollected\s+\d+\s+items?\b", re.IGNORECASE)


def _extract_builtin_failed_tests(gate_output_tail: str) -> list[str]:
    """Parse failing-test identifiers from the built-in test-runner grammar."""
    failed: list[str] = []
    for raw_line in gate_output_tail.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip a parallel-worker prefix, e.g. "[gw7] FAILED tests/...".
        line = _WORKER_PREFIX_RE.sub("", line)
        if line.startswith(("FAILED ", "ERROR ")):
            candidate = line.split()[1].rstrip(":")
            if candidate not in failed:
                failed.append(candidate)
        elif "::" in line and any(token in line.lower() for token in ("failed", "error")):
            candidate = line.split()[0].rstrip(":")
            if candidate not in failed:
                failed.append(candidate)
    return failed


# Xcode's test runner prints a "Failing tests:" block listing one test per
# following indented line, in one of two identifier spellings:
#   - MyAppTests.LoginTests.testInvalidPassword()
#   -[LoginTests testInvalidPassword]
# Neither carries any of the built-in grammar's markers, so an Xcode gate used
# to report "format not recognized" and retry the whole unfocused test target
# every cycle (#2013).
_XCODE_FAILING_HEADER_RE = re.compile(r"^\s*Failing tests:\s*$", re.IGNORECASE)
_XCODE_DOTTED_TEST_RE = re.compile(r"^-\s+([A-Za-z_][\w.]*\.[A-Za-z_]\w*)\s*(?:\(\))?\s*$")
_XCODE_BRACKET_TEST_RE = re.compile(r"^-\[([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\]\s*$")


def _extract_xcode_failed_tests(gate_output_tail: str) -> list[str]:
    """Parse failing-test identifiers from an xcodebuild ``Failing tests:`` block."""
    failed: list[str] = []
    in_block = False
    for raw_line in gate_output_tail.splitlines():
        line = raw_line.strip()
        if _XCODE_FAILING_HEADER_RE.match(raw_line):
            in_block = True
            continue
        if not in_block:
            continue
        if not line:
            # A blank line inside the block is separator noise, not its end.
            continue
        if not line.startswith("-"):
            in_block = False
            continue
        bracket = _XCODE_BRACKET_TEST_RE.match(line)
        if bracket is not None:
            candidate = f"{bracket.group(1)}.{bracket.group(2)}"
        else:
            dotted = _XCODE_DOTTED_TEST_RE.match(line)
            candidate = dotted.group(1) if dotted is not None else ""
        if candidate and candidate not in failed:
            failed.append(candidate)
    return failed


def _output_matches_builtin_grammar(gate_output_tail: str) -> bool:
    """Return whether gate output carries the built-in test runner's summary grammar.

    This is what lets an empty failing-test list from a recognized gate (a
    lint-only failure, say) be told apart from an empty list produced because the
    output was never in the built-in grammar at all. Deliberately narrow: an
    xcodebuild/make style gate ("Testing failed:", "** TEST FAILED **",
    "make[2]: *** [test-ios] Error 65") carries none of these markers.
    """
    for raw_line in gate_output_tail.splitlines():
        line = _WORKER_PREFIX_RE.sub("", raw_line.strip())
        if line.startswith(("FAILED ", "ERROR ", "PASSED ", "SKIPPED ")):
            return True
        if _BUILTIN_SUMMARY_RE.search(line) or _BUILTIN_COLLECTED_RE.search(line):
            return True
    return False


def _extract_with_custom_pattern(gate_output_tail: str, pattern: str) -> list[str]:
    """Extract failing-test identifiers using a project-configured regex.

    The identifier is taken from a named group ``test`` if the pattern declares
    one, else capture group 1, else the whole match. An uncompilable pattern is
    validated at config load, but we guard defensively here too.
    """
    try:
        compiled = re.compile(pattern)
    except re.error:
        return []
    has_test_group = "test" in compiled.groupindex
    failed: list[str] = []
    for raw_line in gate_output_tail.splitlines():
        match = compiled.search(raw_line)
        if not match:
            continue
        if has_test_group:
            candidate = match.group("test")
        elif compiled.groups:
            candidate = match.group(1)
        else:
            candidate = match.group(0)
        candidate = (candidate or "").strip().rstrip(":")
        if candidate and candidate not in failed:
            failed.append(candidate)
    return failed


def extract_failed_tests(
    gate_output_tail: str, failed_test_pattern: str | None = None
) -> FailedTestExtraction:
    """Extract failing-test identifiers from gate output, with an applicability signal.

    A gate command is project configuration; core does not own its output
    format. When ``failed_test_pattern`` is set the project has declared how its
    gate names failures, so extraction always "applies" (recognized) — an empty
    result then means genuinely no failing test. Otherwise core falls back to
    its built-in test-runner grammar (the one emitted by the Python test runner
    this orchestrator ships with); if that grammar is not even present in the
    output, ``format_recognized`` is False so the caller can surface that
    extraction did not apply rather than treating the empty list as a real
    absence.
    """
    if failed_test_pattern:
        tests = _extract_with_custom_pattern(gate_output_tail, failed_test_pattern)
        return FailedTestExtraction(tests=tests, format_recognized=True, source="custom_pattern")
    tests = _extract_builtin_failed_tests(gate_output_tail)
    if tests:
        return FailedTestExtraction(tests=tests, format_recognized=True, source="builtin")
    xcode_tests = _extract_xcode_failed_tests(gate_output_tail)
    if xcode_tests:
        return FailedTestExtraction(tests=xcode_tests, format_recognized=True, source="xcodebuild")
    if _output_matches_builtin_grammar(gate_output_tail):
        return FailedTestExtraction(tests=[], format_recognized=True, source="builtin")
    # Deliberately no "Xcode ran but named nothing" branch: an Xcode gate that
    # fails without a ``Failing tests:`` block (a build break, a lint step) gives
    # no evidence about which tests failed, so claiming a recognized format would
    # turn that silence into a false "no failing tests".
    return FailedTestExtraction(tests=[], format_recognized=False, source="unrecognized")


def _extract_failed_tests(
    gate_output_tail: str, failed_test_pattern: str | None = None
) -> list[str]:
    """Best-effort extraction of failing test identifiers from gate output.

    Thin list-returning wrapper over :func:`extract_failed_tests`; callers that
    need the applicability signal should use the structured function directly.
    """
    return extract_failed_tests(gate_output_tail, failed_test_pattern).tests


def _git_lines(workspace_path: Path, args: Iterable[str]) -> list[str]:
    from . import util as _cu

    cmd = "git " + " ".join(str(arg) for arg in args)
    ok, output = _cu._run_shell(cmd, workspace_path)
    if not ok and not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _retry_review_findings_for_dev_prompt(state: CoordinatorState) -> str | None:
    """Return current actionable findings for a validate-driven retry prompt."""
    return append_convention_retry_findings(
        state.last_review_findings,
        state.convention_violations,
    )


def _entry_gate_note_for_dev_prompt(state: CoordinatorState) -> str | None:
    """Return the dev-prompt note for a gate that ran *before* this run, if any.

    Only a gate that did not finish produces one. A gate that failed outright is
    already described by the ordinary retry channels, and a gate that passed did
    not route the story here. The distinction matters because the two conditions
    ask for different work: a failing test asks the agent to make the change
    correct, a gate that never finished asks whether the work can complete in
    budget — and an agent given the first framing for the second condition
    searches for a broken test in a suite where nothing is broken (#2796).

    Returns None once the note has already reached a prompt: it describes the
    run's entry, not the current iteration, so a later retry must not re-read it
    as a fresh event.
    """
    outcome = state.entry_gate_outcome
    if outcome is None or outcome.outcome != "timeout" or state.entry_gate_surfaced_to_dev:
        return None

    parts = [
        f"The gate (`{outcome.command}`) was run on this branch before you were invoked, and"
        f" **it did not finish**: it was killed at its {outcome.timeout_s}s time budget after"
        f" {outcome.elapsed_s:.1f}s. That is why you are running now."
        "\n\n"
        "**No test failed.** The run never reached a summary, so there is no failing test to"
        " find and no list of failures to work from. Do not go looking for a broken assertion —"
        " the branch's own suite is not known to be red.",
        f"Configured gate_timeout: {outcome.timeout_s}s",
        f"Elapsed before the gate was killed: {outcome.elapsed_s:.1f}s",
    ]
    if outcome.profile:
        parts.append(f"Validation profile: {outcome.profile}")
    parts.append(
        "Your job is to make the gate finish inside that budget. RCA which test or product-code"
        " path is hanging or has become slow enough to exhaust it, and fix the underlying cause."
        " Do not raise the timeout or delete coverage as the first move — the wall-clock guard is"
        " intentional. If the suite has genuinely outgrown its budget with no single culprit, say"
        " so explicitly in your handoff, with the measurements that support it, rather than"
        " reporting that you found nothing wrong."
    )
    tail = (outcome.output_tail or "").strip()
    parts.append(
        f"Gate output tail (truncated at the kill boundary, so it ends mid-run):\n{tail}"
        if tail
        else "The gate produced no captured output before it was killed."
    )
    return "\n\n".join(parts)


def _gate_output_fingerprint(
    gate_output_digest: str | None, gate_result: str | None
) -> str | None:
    """Return the stall-detection fingerprint for a *failing* gate run.

    ``gate_output_digest`` is ``run_gate_full``'s SHA-256 of the full output.
    Only failing runs are fingerprinted: a passing gate prints the same thing
    every time, so hashing it would make two consecutive PASSes look like a
    stalled failure.

    Returns None whenever no digest was produced (gate skipped, or the runner
    stubbed), which fails open — the brake needs two adjacent equal fingerprints,
    so an absent one simply lets the retry proceed. That is the safe direction: a
    false non-match costs one more dev iteration, a false match kills a story
    that was still converging.
    """
    if not gate_output_digest or gate_result in (None, "PASS"):
        return None
    return gate_output_digest


def record_dev_iteration_telemetry(
    state: CoordinatorState,
    workspace_path: Path,
    *,
    max_iterations: int,
    gate_result: str | None,
    gate_output_tail: str = "",
    gate_output_digest: str | None = None,
    is_timeout: bool = False,
    runner_failure_summary: str | None = None,
    failed_test_pattern: str | None = None,
) -> None:
    """Capture per-iteration dev telemetry after validation completes."""
    if not state.dev_results or not state.dev_durations:
        return
    iteration = state.dev_iteration
    attempt_count = state.pending_dev_transport_retry_count + 1
    dev_attempts = state.dev_results[-attempt_count:]
    duration_attempts = state.dev_durations[-attempt_count:]
    dev_result = dev_attempts[-1]
    duration_s = sum(duration_attempts)
    # None-aware: a MIX of measured and unmeasured attempts is still unknown —
    # summing only the measured ones would report a partial subtotal as if it
    # were this iteration's cost (#1992).
    cost_usd = sum_costs(result.cost_usd for result in dev_attempts)
    baseline = state.last_dev_start_commit or "HEAD"
    files_changed = _git_lines(workspace_path, ["diff", "--name-only", baseline, "HEAD"])
    dirty_files = [
        line.split(maxsplit=1)[-1]
        for line in _git_lines(workspace_path, ["status", "--porcelain"])
    ]
    for dirty in dirty_files:
        if dirty not in files_changed:
            files_changed.append(dirty)

    extraction = extract_failed_tests(gate_output_tail, failed_test_pattern)
    failed_tests = extraction.tests
    # None when there was no failing-gate output to parse; otherwise the
    # applicability signal that separates a genuine empty list from a
    # silently-unrecognized gate format.
    format_recognized = extraction.format_recognized if gate_output_tail else None
    prev_failed = (
        state.dev_iteration_telemetry[-1].failed_tests if state.dev_iteration_telemetry else []
    )
    tests_fixed_count = len(set(prev_failed) - set(failed_tests)) if prev_failed else 0
    meaningful_progress = bool(files_changed or tests_fixed_count > 0)
    state.dev_iteration_telemetry.append(
        DevIterationTelemetry(
            iteration=iteration,
            max_iterations=max_iterations,
            cost_usd=cost_usd,
            duration_s=duration_s,
            cycle=state.review_cycle,
            gate_result=gate_result,
            failed_tests=failed_tests,
            gate_output_format_recognized=format_recognized,
            gate_output_fingerprint=_gate_output_fingerprint(gate_output_digest, gate_result),
            existing_test_failures=False,
            is_timeout=is_timeout,
            files_changed=files_changed,
            files_changed_count=len(files_changed),
            tests_fixed_count=tests_fixed_count,
            meaningful_progress=meaningful_progress,
            sandboxed=state.sandboxed,
            containment=state.dev_containment,
            sandbox_capabilities=dict(state.dev_sandbox_capabilities),
            agent_exit_code=dev_result.exit_code,
            runner_failure_code=dev_result.failure_code,
            runner_failure_summary=runner_failure_summary,
            cli_quota_error_observed=dev_result.cli_quota_error_observed,
            transport_fallback_fired=dev_result.transport_fallback_fired,
            transport_fallback_reason=dev_result.transport_fallback_reason,
            transport_used=dev_result.transport_used,
            model_used=dev_result.model_used,
            transport_retry_count=state.pending_dev_transport_retry_count,
            transport_retry_events=list(state.pending_dev_transport_retry_events),
            verification_requests=list(state.pending_dev_verification_requests),
        )
    )
    state.pending_dev_transport_retry_count = 0
    state.pending_dev_transport_retry_events = []
    state.pending_dev_verification_requests = []


def _still_open_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return still-open P1 findings for carry-forward prompt context."""
    open_dispositions = {
        "unresolved",
        "net_new",
        "corroborated_new",
        "regression",
        "ac_blocking",
    }
    return [
        record
        for record in state.finding_registry
        if record.severity == "P1" and record.disposition in open_dispositions
    ]


def _prior_open_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return still-open P1s that predate the most recent review cycle."""
    if state.review_cycle <= 1:
        return []
    return [
        record
        for record in _still_open_p1s_for_dev_prompt(state)
        if record.cycle_first_seen < state.review_cycle
    ]


def _current_cycle_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return classified P1s from the most recent review cycle.

    diff_ungrounded records are excluded: they name code this story's diff does
    not touch, so presenting them as work would send the dev agent after a change
    it cannot make (#2525). They remain in the registry and in the audit's
    non_blocking_p1s, which is where a suppressed finding is looked for.
    """
    if state.review_cycle <= 0:
        return []
    return [
        record
        for record in state.finding_registry
        if record.severity == "P1"
        and record.cycle_last_seen == state.review_cycle
        and record.disposition != "diff_ungrounded"
    ]


def _ensure_runners() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _capture_dev_handoff(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    dev_result: object,
) -> Path | None:
    """Capture the handoff snapshot from a completed dev agent result.

    Writes the forge artifact when structured output is present; falls back to
    reading the workspace handoff file. Appends to state.dev_handoff_snapshots.
    Returns the forge artifact path when written, else None.
    """
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(dev_result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(dev_result)}")
    if dev_result.dev_handoff is not None:
        _forge_handoff_dir = config.project_root / ".forge" / "handoffs" / task.slug
        _forge_handoff_dir.mkdir(parents=True, exist_ok=True)
        _forge_artifact_path = _forge_handoff_dir / f"iter_{state.dev_iteration}.yaml"
        try:
            _forge_artifact_path.write_text(
                yaml.dump(dev_result.dev_handoff, allow_unicode=True, default_flow_style=False),
                encoding="utf-8",
            )
            _log(f"  Handoff artifact: {_forge_artifact_path}")
        except Exception as _write_exc:  # noqa: BLE001
            _log(f"  ⚠ Failed to write handoff artifact: {_write_exc}")
        state.dev_handoff_snapshots.append(
            {
                "source": "structured_output",
                "path": str(_forge_artifact_path),
                "handoff": dev_result.dev_handoff,
            }
        )
        return _forge_artifact_path
    else:
        state.dev_handoff_snapshots.append({"source": "missing", "path": None, "handoff": None})
        return None


def _rollback_recorded_dev_attempt(
    state: CoordinatorState,
    *,
    dev_results_len: int,
    dev_durations_len: int,
    dev_handoff_len: int,
) -> None:
    """Remove only the trailing no-judgment DEV attempt from ordinary accounting."""
    if len(state.dev_results) > dev_results_len:
        state.dev_results.pop()
    if len(state.dev_durations) > dev_durations_len:
        state.dev_durations.pop()
    if len(state.dev_handoff_snapshots) > dev_handoff_len:
        state.dev_handoff_snapshots.pop()
    state.pending_dev_transport_retry_count = 0
    state.pending_dev_transport_retry_events = []


def _pending_dev_transport_retry_failure_extra(state: CoordinatorState) -> dict:
    """Audit-visible transport-retry evidence for a DEV failure record."""
    if (
        state.pending_dev_transport_retry_count <= 0
        and not state.pending_dev_transport_retry_events
    ):
        return {}
    return {
        "transport_retry_count": state.pending_dev_transport_retry_count,
        "transport_retry_events": list(state.pending_dev_transport_retry_events),
    }


def _resolve_dev_sandbox_capabilities(config: ForgeConfig) -> dict:
    """Resolve the project's sandbox capability declaration for audit + logging.

    Returns the audit payload for the capabilities the dev run will be granted:
    the selected preset (#1947) merged with the project's own additive
    ``sandbox.write_roots``/``sandbox.mach_services`` grants (#2038). With
    nothing declared this is an explicit null/empty payload — default
    containment, recorded rather than omitted.

    A declaration this host's sandbox backend cannot express resolves to *empty*
    grants (the runner refuses the run), so the audit trail never claims a
    capability that was not actually applied. What was asked for is kept under
    ``requested_profile``/``requested_write_roots``/``requested_mach_services``
    so the refusal is diagnosable from the audit record alone.
    """
    requested = config.sandbox.capability_profile
    requested_roots = config.sandbox.write_roots
    requested_services = config.sandbox.mach_services
    try:
        return resolve_capabilities(
            requested,
            system=platform.system(),
            write_roots=requested_roots,
            mach_services=requested_services,
        ).audit_payload()
    except SandboxCapabilityError as exc:
        _log(
            f"  WARNING: the declared sandbox capabilities (profile {requested!r}, "
            f"write_roots {list(requested_roots)}, mach_services "
            f"{list(requested_services)}) are not usable on this host — the dev run "
            f"will fail closed. {exc}"
        )
        payload = resolve_capabilities(None).audit_payload()
        payload["requested_profile"] = requested
        payload["requested_write_roots"] = list(requested_roots)
        payload["requested_mach_services"] = list(requested_services)
        payload["unsupported_reason"] = str(exc)
        return payload


def _dev_prompt_builder(task: TaskStory) -> "Callable[..., str]":
    """Pick the dev prompt builder for this task.

    A batch-group leader gets the multi-spec builder; every other story keeps
    the single-story one. Selected by the presence of ``batch_members`` rather
    than by a flag so an unbatched run cannot accidentally take the batch path.
    """
    if task.batch_members:
        return functools.partial(build_batch_dev_prompt, members=task.batch_members)
    return build_dev_prompt


def _run_dev_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: StructuredLogger | None,
    stop_event: "threading.Event | None" = None,
) -> CoordinatorResult | None:
    """Run one DEV iteration. Returns CoordinatorResult on budget escalation, else None.

    Caller must increment state.dev_iteration and _dev_calls_this_cycle before calling.
    Mutates state in-place (appends dev_results, updates dev_session_id, etc.).
    """
    _ensure_runners()
    # Cost-aware batch group (#727): the shared dev pass is told about every
    # member story, not just the leader whose slug owns the worktree. Everything
    # downstream of the prompt — validation, review, cost, audit — stays
    # per-story; only the DEV assignment is shared.
    if task.batch_members:
        story_content = render_batch_spec_section(task.batch_members)
    state.pending_dev_transport_retry_count = 0
    state.pending_dev_transport_retry_events = []
    state.pending_dev_verification_requests = []
    # Probe sandbox availability once per run (lru_cache-backed — cheap on repeat calls).
    # `sandboxed` is the mechanical-containment bool; `dev_containment` records the
    # richer mode so audit/status distinguishes a wrapped run from a prompt-only one.
    state.sandboxed = sandbox_available_for_profile(config.dev_profile)
    state.dev_containment = sandbox_containment_mode(config.dev_profile)
    state.dev_sandbox_capabilities = _resolve_dev_sandbox_capabilities(config)
    if config.dev_profile.mode == "cli" and config.dev_profile.sandbox_mode == "none":
        _log(
            "  WARNING: sandbox_mode: none — dev agent runs without write containment. "
            "Use for debugging only."
        )
    elif state.dev_containment == "unavailable":
        _log(
            "  WARNING: sandbox_mode requested but the host sandbox (sandbox-exec/bwrap) "
            "is unavailable — dev run will fail closed rather than run prompt-only."
        )
    elif state.dev_containment == "mechanical":
        _log("  dev write containment: mechanical (host sandbox wrapper)")
    _capabilities = state.dev_sandbox_capabilities
    # Log whenever *anything* was declared — a project's inline grants apply
    # with no preset selected, so keying the log on 'profile' alone would hide
    # the inline-only case from the operator entirely (#2038).
    _declared = any(_capabilities.get(key) for key in ("profile", "write_roots", "mach_services"))
    if _declared:
        _log(
            f"  sandbox capability profile: {_capabilities['profile'] or '(none)'} "
            f"({len(_capabilities['write_roots'])} extra write roots, "
            f"{len(_capabilities['mach_services'])} mach services; "
            f"{len(_capabilities.get('project_write_roots', []))} roots and "
            f"{len(_capabilities.get('project_mach_services', []))} services "
            "declared by the project)"
        )
    elif _capabilities.get("unsupported_reason"):
        _log(
            "  sandbox capability declaration refused on this host: "
            f"{_capabilities['unsupported_reason']}"
        )
    _preserve_error_type = state.error_type == "max_iterations_no_submit"
    if not _preserve_error_type:
        state.error_type = None
    _log_phase(
        state.phase,
        f"{config.dev_profile.model}  iter={state.dev_iteration}",
    )
    if logger:
        logger._safe_emit("phase_start", phase="DEV", iteration=state.dev_iteration)

    # ── Workspace hygiene gate (first DEV entry only) ─────────────────
    # Reject or sanitise the worktree before the dev agent sees it for the
    # first time. Stray untracked files at repo root (left behind by tracked-
    # leftover files in main, or by a non-DEV phase that wrote where it
    # shouldn't) silently sabotage dev runs — see issue #1179. Quarantine
    # non-destructively into .forge/quarantine/<run-id>/iter-<n>/ so
    # operators can recover originals.
    #
    # Iterations 2+ are not re-gated: validate-phase's auto-commit owns
    # cleanup after the first iteration, and intermediate dirty state on a
    # retry is a legitimate handoff between iterations rather than a stray
    # phase mutation.
    from .workspace_hygiene import enforce_pre_dev_hygiene, ensure_scratch_dir  # noqa: PLC0415

    _hygiene_run_id = state.run_id or "unknown"
    ensure_scratch_dir(workspace_path, _hygiene_run_id)
    if state.dev_iteration <= 1:
        _hygiene_ok, _hygiene_diag, _hygiene_audit = enforce_pre_dev_hygiene(
            workspace_path,
            _hygiene_run_id,
            iteration=state.dev_iteration,
        )
        state.workspace_hygiene_audit.append({"phase": "PRE_DEV", **_hygiene_audit})
        if _hygiene_audit.get("quarantined"):
            _q_paths = ", ".join(_hygiene_audit["quarantined"])
            _q_dir = _hygiene_audit.get("quarantine_dir")
            _log(f"  ⚠ DEV   quarantined stray paths to {_q_dir}: {_q_paths}")
        if not _hygiene_ok:
            state.phase = Phase.ESCALATE
            state.error = _hygiene_diag or "Workspace hygiene gate refused DEV entry"
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="DEV", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="DEV")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

    # Capture HEAD before the dev agent runs — used by finding_classifier for git diff.
    # Best-effort: any failure is silently ignored (non-critical for correctness).
    try:
        _head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
        )
        if _head_proc.returncode == 0:
            state.last_dev_start_commit = _head_proc.stdout.decode().strip()
    except Exception:  # noqa: BLE001  # best-effort, any error is harmless
        pass

    _gate_cmd = (
        task.gate_override
        if task.gate_override is not None and not _is_gate_skip(task.gate_override)
        else config.validation.gate_command
    )
    # The dev/fix inner loop gets an *advisory* profile: scoped if the project
    # declared one, else its cheap broad one, else — widening, never narrowing —
    # the complete profile. Selection runs through the one path VALIDATE also
    # uses, so what an agent is told to run and what decides the merge cannot
    # drift apart (#2358).
    _advisory = select_validation(config.validation, phase=PHASE_ADVISORY, task=task)
    _merge_profile = merge_profile(config.validation)
    # A legacy project with no test_command widens to the complete profile, and
    # its prompt must stay byte-identical to what it produced before profiles
    # existed: the unresolved gate command, which the prompt then suppresses the
    # testing section for.
    _legacy_widened = _advisory.widened and not _advisory.declared
    _test_cmd = _gate_cmd if _legacy_widened else _advisory.command
    _profile_prompt_kwargs: dict = (
        {}
        if _legacy_widened
        else {
            "test_profile": _advisory.profile,
            "test_authority": _advisory.authority,
            "gate_profile": _merge_profile.name,
        }
    )
    _log_verbose(
        f"  validation profile (dev): {_advisory.describe()} → {_test_cmd}"
        + ("  [widened: no advisory profile matched]" if _advisory.widened else "")
    )
    _dev_entry_reason = state.retry_reason  # snapshot before consumed by prompt routing
    # ── Dev-phase verification capability (ADR-0007 / #2050) ──────────────
    # A project whose inner-loop toolchain the host sandbox structurally cannot
    # host declares whole named commands in forge.yaml. The broker is built here
    # — before prompt routing, because the prompt must name the request channel —
    # started immediately before the agent runs, and stopped in a finally after
    # it returns. Its lifetime spans the whole iteration *including* transport
    # retries, so the per-iteration request budget does not reset when run_agent
    # is re-attempted. With nothing declared no broker exists, no channel
    # directory is created, and the prompt says nothing about the capability.
    _verification_broker: DevVerificationBroker | None = None
    if config.validation.dev_verification_commands:
        _verification_broker = DevVerificationBroker(
            workspace_path=workspace_path,
            commands=config.validation.dev_verification_commands,
            iteration=state.dev_iteration,
            max_requests=config.validation.dev_verification_max_requests,
            expected_python=config.workspace.python_interpreter,
        )
        _log(
            f"  dev verification commands: {', '.join(_verification_broker.command_names)} "
            f"(max {_verification_broker.max_requests} requests this iteration)"
        )
    # One description of the channel, in the renderer's own vocabulary. The
    # prompt builders take it under ``verification_``-prefixed names; the
    # timeout-resume route (which has no builder) renders the section directly
    # from the same dict, so the two routes cannot describe different channels.
    _verification_channel: dict = (
        {
            "commands": tuple(
                (entry.name, entry.command)
                for entry in config.validation.dev_verification_commands
            ),
            "request_dir": str(_verification_broker.request_dir),
            "response_dir": str(_verification_broker.response_dir),
            "max_requests": _verification_broker.max_requests,
        }
        if _verification_broker is not None
        else {}
    )
    _verification_prompt_kwargs: dict = {
        f"verification_{key}": value for key, value in _verification_channel.items()
    }
    # ── Specification-gap backchannel (#2122) ─────────────────────────────
    # Every prompt-building route gets the same two facts: how many gap pauses
    # this run can still honour, and every gap already settled for this story
    # (including ones an earlier run or an earlier iteration answered). Assembled
    # once, before routing, so no route can describe a different channel — and
    # so the timeout-resume route, which builds its prompt without a builder,
    # renders the resolved gaps from the same source.
    _spec_gap_prompt_kwargs: dict = {
        "spec_gap_pauses_remaining": spec_gap_pauses_remaining(state, config),
        "resolved_spec_gaps": resolved_gaps_for_prompt(state),
    }
    # Gate execution is coordinator-owned on EVERY iteration (#1944 / #823). The
    # dev agent is never the gate authority: VALIDATE runs the authoritative gate
    # unsandboxed (on every successful handoff before REVIEW/MERGE), or records a
    # skipped gate (gate_override: none) as PASS. The agent — which runs inside a
    # write-containment sandbox that denies the process/build operations many gates
    # exercise — is therefore never asked to run or prove the gate, and the
    # unproven-completion guard accepts a MET-without-PASS handoff and defers to
    # VALIDATE for the authoritative result. Set once, unconditionally — True for
    # skipped gates too, since VALIDATE passes them, and True on MAX_ITERATIONS/
    # TIMEOUT retries, which also reach VALIDATE (retry_reason is reset below). A
    # per-case flag invites omitting a routing path and recreating the false
    # HANDOFF_NO_GATE_EVIDENCE trap; the guard's escalate branch is retained as a
    # fail-closed backstop should any future path leave this False.
    state.gate_delegated_this_iteration = True
    match state.retry_reason:
        case RetryReason.TIMEOUT_RESUME:
            prompt = (
                state.human_feedback
                or "You were cut off by a timeout. Continue from where you left off."
            )
            # A timeout resume is the one route that does not go through a prompt
            # builder, so the verification section has to be appended here. It is
            # not optional: the channel is per-iteration, so the paths the agent
            # was given before the timeout are stale, and a resumed agent left
            # with only the continuation text would poll a directory the
            # coordinator is no longer serving — indistinguishable, from inside
            # the agent, from the capability not existing at all.
            prompt += render_verification_section(**_verification_channel)
            # Same reasoning as the verification section above, applied to the
            # gap channel: a resumed agent that is not told what an operator
            # already decided will rediscover the question this run paid to
            # answer — and, with the channel still open, may ask it again.
            prompt += render_resolved_spec_gaps_section(
                _spec_gap_prompt_kwargs["resolved_spec_gaps"]
            )
            prompt += render_spec_gap_section(
                remaining_pauses=_spec_gap_prompt_kwargs["spec_gap_pauses_remaining"]
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.retry_reason = None
            state.human_feedback = None
        case RetryReason.P2_CLEANUP:
            prompt = build_fix_prompt(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                review_findings=state.last_review_findings or "No specific findings provided.",
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                **_profile_prompt_kwargs,
                gate_skipped=_is_gate_skip(task.gate_override),
                iteration=state.dev_iteration,
                cycle_history=state.cycle_history or None,
                escalation_note=state.escalation_note,
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                prior_open_p1s=None,
                classified_p1s=None,
                surviving_families=None,
                conventions=config.conventions_soft,
                advisory_p2_only=True,
                p2_policy=config.dev.p2_policy,
                **_verification_prompt_kwargs,
                **_spec_gap_prompt_kwargs,
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.escalation_note = None
        case RetryReason.REVIEW_CHANGES | RetryReason.EXTEND if state.last_review_findings:
            carry_forward_p1s = _prior_open_p1s_for_dev_prompt(state)
            current_cycle_p1s = _current_cycle_p1s_for_dev_prompt(state)
            prompt = build_fix_prompt(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                review_findings=state.last_review_findings,
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                **_profile_prompt_kwargs,
                gate_skipped=_is_gate_skip(task.gate_override),
                iteration=state.dev_iteration,
                cycle_history=state.cycle_history or None,
                escalation_note=state.escalation_note,
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                prior_open_p1s=carry_forward_p1s or None,
                classified_p1s=current_cycle_p1s or None,
                surviving_families=state.surviving_families or None,
                conventions=config.conventions_soft,
                p2_policy=config.dev.p2_policy,
                **_verification_prompt_kwargs,
                **_spec_gap_prompt_kwargs,
            )
            injected_finding_ids = [r.finding_id for r in carry_forward_p1s]
            injected_finding_ids.extend(
                r.finding_id for r in current_cycle_p1s if r.finding_id not in injected_finding_ids
            )
            state.dev_prompt_injected_finding_ids.append(injected_finding_ids)
            state.escalation_note = None  # consumed
        case RetryReason.MAX_ITERATIONS_NO_SUBMIT:
            dev_context = ContextAssembler.from_config(config).assemble(
                phase="dev",
                story_text=story_content,
                file_list=plan_file_list(state.plan_structured) or None,
            )
            state.context_manifests.append({"phase": "dev", "manifest": dev_context})
            prompt = _dev_prompt_builder(task)(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                story_content=story_content,
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                **_profile_prompt_kwargs,
                gate_skipped=_is_gate_skip(task.gate_override),
                review_findings=_retry_review_findings_for_dev_prompt(state),
                human_feedback=state.human_feedback,
                preflight_output=(
                    state.preflight_result.output if state.preflight_result else None
                ),
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                plan_review_advisory=state.plan_agent_review_findings,
                iteration=state.dev_iteration,
                escalation_note=state.escalation_note,
                cycle_history=state.cycle_history or None,
                preflight_sufficiency=state.preflight_sufficiency,
                contract_change=state.preflight_contract_change,
                conventions=config.conventions_soft,
                assembled_context=dev_context,
                p2_policy=config.dev.p2_policy,
                **_verification_prompt_kwargs,
                **_spec_gap_prompt_kwargs,
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.escalation_note = None
        case (
            None
            | RetryReason.GATE_FAIL
            | RetryReason.CONVENTION_VIOLATIONS
            | RetryReason.DIRTY_WORKTREE
            | RetryReason.REJECT
            | RetryReason.EXTEND
            | RetryReason.REVIEW_CHANGES
            | RetryReason.SPEC_GAP_RESUME
        ):
            # None → first iteration; gate_fail/convention_violations/dirty_worktree/reject
            # /extend(no findings) → fresh dev prompt.
            #
            # spec_gap_resume takes this route rather than a short continuation
            # prompt of its own: the resolved gap is rendered by the same
            # builder every other route uses, so the answer reaches the agent
            # whether or not its session survived the pause. A continuation
            # prompt would carry the answer only when the session was reusable,
            # and lose it exactly when the agent has the least context (#2122).
            #
            # This is also the branch a sprint-resume entry lands on, so it is
            # where a gate that ran before the run and did not finish has to be
            # named. Read before the prompt is built and marked as surfaced
            # after, so the fact reaches exactly one prompt (#2796).
            _entry_gate_note = _entry_gate_note_for_dev_prompt(state)
            dev_context = ContextAssembler.from_config(config).assemble(
                phase="dev",
                story_text=story_content,
                file_list=plan_file_list(state.plan_structured) or None,
            )
            state.context_manifests.append({"phase": "dev", "manifest": dev_context})
            prompt = _dev_prompt_builder(task)(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                story_content=story_content,
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                **_profile_prompt_kwargs,
                gate_skipped=_is_gate_skip(task.gate_override),
                review_findings=_retry_review_findings_for_dev_prompt(state),
                human_feedback=state.human_feedback,
                preflight_output=(
                    state.preflight_result.output if state.preflight_result else None
                ),
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                plan_review_advisory=state.plan_agent_review_findings,
                iteration=state.dev_iteration,
                escalation_note=state.escalation_note,
                cycle_history=state.cycle_history or None,
                preflight_sufficiency=state.preflight_sufficiency,
                contract_change=state.preflight_contract_change,
                conventions=config.conventions_soft,
                assembled_context=dev_context,
                p2_policy=config.dev.p2_policy,
                # The first dev prompt built for this run is the one that can
                # still act on an inherited tree; consumed below so later
                # iterations, which are looking at their own work, do not
                # re-read the warning as though it were about them (#2288).
                inherited_work_note=state.workspace_inherited_work_note,
                # Why this run entered at DEV at all, when the gate that decided
                # it did not finish. One-shot for the same reason (#2796).
                entry_gate_note=_entry_gate_note,
                **_verification_prompt_kwargs,
                **_spec_gap_prompt_kwargs,
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.escalation_note = None  # consumed
            if state.workspace_inherited_work_note is not None:
                # Sticky record that the warning reached an agent, kept because
                # the note itself is cleared on the next line.
                state.workspace_inherited_work_surfaced_to_dev = True
            state.workspace_inherited_work_note = None  # consumed
            if _entry_gate_note is not None:
                # Sticky record that the entry gate's outcome reached an agent.
                # The outcome itself stays on the state for the audit; this is
                # what stops a later iteration re-reading it.
                state.entry_gate_surfaced_to_dev = True
        case _:
            raise ValueError(f"Unrecognized retry_reason: {state.retry_reason!r}")
    state.retry_reason = None  # consumed

    write_trace(
        workspace_path / ".forge/traces" / f"{state.dev_trace_count}-dev-prompt.txt",
        prompt,
    )

    _resolved_timeout, _dev_override_active = resolve_timeout_with_active(
        config.dev_profile.timeout_seconds,
        config.dev_profile.timeout_medium_seconds,
        config.dev_profile.timeout_large_seconds,
        state.preflight_complexity,
        state.preflight_complexity_score,
    )
    # ── Fit this invocation inside the story deadline containing it (#2333) ───
    # Two guards, because the seated value is not always present: when
    # adaptive_dev_timeout_seconds is unset the fallback is the raw
    # complexity-derived figure, which knows nothing of the enclosing ceiling, so
    # it gets the same static cap the seated value received. Then both are
    # clamped against what the story has actually got left.
    _enclosing_budget = _worker_budget.current_worker_budget()
    if state.adaptive_dev_timeout_seconds:
        _dev_timeout = state.adaptive_dev_timeout_seconds
    else:
        _dev_timeout, _fallback_cap_audit = cap_timeout_to_story_ceiling(
            _resolved_timeout,
            (_enclosing_budget.worker_timeout_seconds if _enclosing_budget is not None else None),
            max(1, state.adaptive_dev_max or config.retry.max_dev_iterations),
        )
        if _fallback_cap_audit["capped"]:
            _log(f"  {_fallback_cap_audit['rationale']}")
    _dev_timeout, _clamp_audit = clamp_timeout_to_remaining(
        _dev_timeout,
        _enclosing_budget.remaining() if _enclosing_budget is not None else None,
    )
    if _clamp_audit is not None:
        state.dev_timeout_clamps.append(_clamp_audit)
        _log(f"  {_clamp_audit['rationale']}")
    if _dev_timeout <= 0:
        # The story's deadline is already spent. Launching an agent here would
        # buy a process, a prompt, and a model call that cannot outlive the next
        # scheduler poll — and the invocation would be SIGKILLed mid-work with no
        # measurable cost, which is the failure shape this story is about. Refuse
        # instead, and say so in terms of the deadline rather than the work.
        state.phase = Phase.ESCALATE
        state.error = (
            "Story deadline exhausted before this development invocation could start "
            f"(iteration {state.dev_iteration}); refusing to spend on an invocation the "
            "enclosing worker window cannot contain"
        )
        state.error_type = "TimeoutError"
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="DEV", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="DEV")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )
    if _dev_override_active:
        _log(f"  Dev timeout: {_dev_timeout}s ({state.preflight_complexity} complexity)")
    else:
        _log(f"  Dev timeout: {_dev_timeout}s")
    _plan_files = _plan_files_for_stuck_scaling(state, logger)
    _scaled_stuck = _scale_stuck_for_complexity(
        config.stuck_detection,
        state.preflight_complexity,
        len(_plan_files),
    )
    if (
        _scaled_stuck.no_progress_iterations != config.stuck_detection.no_progress_iterations
        or _scaled_stuck.post_nudge_iterations != config.stuck_detection.post_nudge_iterations
    ):
        _log_verbose(
            f"  Stuck-detection scaled for {state.preflight_complexity} "
            f"({len(_plan_files)} plan files): "
            f"no_progress={_scaled_stuck.no_progress_iterations} "
            f"(base {config.stuck_detection.no_progress_iterations}), "
            f"post_nudge={_scaled_stuck.post_nudge_iterations} "
            f"(base {config.stuck_detection.post_nudge_iterations})"
        )
    _dev_profile = _dc_replace(
        config.dev_profile,
        timeout_seconds=_dev_timeout,
        max_iterations=state.adaptive_dev_max or config.dev_profile.max_iterations,
        stuck_detection=_scaled_stuck,
        sandbox_capability_profile=config.sandbox.capability_profile,
        sandbox_write_roots=config.sandbox.write_roots,
        sandbox_mach_services=config.sandbox.mach_services,
    )

    _dev_total_start = time.monotonic()
    _dev_results_this_iteration = []
    _dev_durations_this_iteration = []
    _dev_results_before = len(state.dev_results)
    _dev_durations_before = len(state.dev_durations)
    _dev_handoff_before = len(state.dev_handoff_snapshots)
    _runner_failure = None
    _current_session_id = state.dev_session_id
    _dev_retry_events: list[dict] = []
    _max_transport_retries = max(0, config.retry.max_dev_transport_retries)

    if _verification_broker is not None:
        _verification_broker.start()
    try:
        while True:
            _attempt_start = time.monotonic()
            dev_result = run_agent(
                prompt=prompt,
                profile=_dev_profile,
                working_dir=workspace_path,
                session_id=_current_session_id,
                secrets=config.secrets,
                stop_event=stop_event,
            )
            _attempt_elapsed = time.monotonic() - _attempt_start
            _runner_failure = None
            if not dev_result.success and not dev_result.startup_failure:
                _runner_failure = classify_runner_subprocess_failure(
                    dev_result.output, dev_result.exit_code
                )
                if _runner_failure is not None:
                    dev_result = _dc_replace(dev_result, failure_code=_runner_failure[0])

            if len(_dev_retry_events) < _max_transport_retries and _is_transient_dev_failure(
                dev_result, _runner_failure
            ):
                retry_count = len(_dev_retry_events) + 1
                _failure_summary = _summarize_dev_transport_failure(dev_result)
                _dev_results_this_iteration.append(dev_result)
                _dev_durations_this_iteration.append(_attempt_elapsed)
                _dev_retry_events.append(
                    {
                        "iteration": state.dev_iteration,
                        "retry": retry_count,
                        "error": _failure_summary,
                    }
                )
                _log(
                    f"  ↻ DEV   transient transport failure "
                    f"(retry {retry_count}/{_max_transport_retries})"
                )
                if state.log_dir is not None:
                    write_trace(
                        state.log_dir
                        / (
                            f"dev-iter-{state.dev_iteration}-{config.dev_profile.name}"
                            f"-retry{retry_count}.log"
                        ),
                        dev_result.output or "",
                    )
                _current_session_id = dev_result.session_id if _dev_profile.mode == "cli" else None
                _backoff_s = _dev_transport_retry_backoff_seconds(retry_count)
                _log_verbose(f"  DEV retry backoff: {_backoff_s}s")
                time.sleep(_backoff_s)
                continue

            _dev_results_this_iteration.append(dev_result)
            _dev_durations_this_iteration.append(_attempt_elapsed)
            _dev_elapsed = time.monotonic() - _dev_total_start
            break
    finally:
        if _verification_broker is not None:
            _verification_broker.stop()
            _served = _verification_broker.records()
            state.pending_dev_verification_requests = _served
            state.dev_verification_requests.extend(_served)
            if _served and logger:
                for _req in _served:
                    logger._safe_emit(
                        "dev_verification_request",
                        phase="DEV",
                        **_req,
                    )

    state.pending_dev_transport_retry_count = len(_dev_retry_events)
    state.pending_dev_transport_retry_events = list(_dev_retry_events)
    write_trace(
        workspace_path / ".forge/traces" / f"{state.dev_trace_count}-dev-output.txt",
        dev_result.output,
    )
    # Write dev iteration log to durable story log dir
    if state.log_dir is not None:
        write_trace(
            state.log_dir / f"dev-iter-{state.dev_iteration}-{config.dev_profile.name}.log",
            dev_result.output or "",
        )
    state.dev_results.extend(_dev_results_this_iteration)
    state.dev_durations.extend(_dev_durations_this_iteration)
    _capture_dev_handoff(state, config, task, workspace_path, dev_result)
    if _dev_profile.mode == "cli" and dev_result.transport_used == "api":
        state.dev_session_id = None
    elif dev_result.session_id and produced_model_output(dev_result):
        state.dev_session_id = dev_result.session_id
    save_sessions(workspace_path, state.dev_session_id, state.reviewer_session_ids)
    log_agent_result(dev_result, "DEV")
    _dev_cost_total = sum_costs(result.cost_usd for result in _dev_results_this_iteration)
    _dev_cost_str = _fmt_cost(_dev_cost_total)
    # Glyph reflects the actual outcome: a killed or crashed iteration must not
    # be rendered with the success glyph one line above the failure it caused.
    _dev_glyph = "✓" if dev_result.success else "✗"
    _log(f"  {_dev_glyph} DEV   {_dev_cost_str}  {_fmt_duration(_dev_elapsed)}")
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="DEV",
            outcome="success" if dev_result.success else "failure",
            cost_usd=_dev_cost_total,
            duration_s=round(_dev_elapsed, 2),
        )

    # ── Specification gap raised by this iteration (#2122) ───────────
    # Checked before every other outcome path: a dev agent that stopped to ask
    # about an underspecified criterion has not failed, has not completed, and
    # must not be routed as either. Handled here — at the moment of ambiguity —
    # rather than after the review allowance is exhausted, which is the whole
    # point: the operator answers one question instead of the run spending a
    # review budget rediscovering that the guess was wrong.
    #
    # The gap resolution never touches review_cycle or
    # validate_opened_review_cycles, so no review cycle is spent on the gap
    # itself; the coordinator loops straight back to DEV.
    if handle_spec_gap(
        state,
        config,
        task,
        dev_result.output,
        logger=logger,
    ):
        # Preserve whatever the agent produced before it stopped to ask. Same
        # reasoning as the timeout and max-iterations paths: uncommitted work is
        # invisible to the next iteration's baseline, and the coordinator is the
        # party that owns the worktree.
        if _worktree_has_changes(workspace_path):
            if _checkpoint_commit(workspace_path, "specification gap raised"):
                _log("  ⎇ DEV   checkpoint-committed work before the specification-gap pause")
                if logger:
                    logger._safe_emit(
                        "dev_checkpoint_commit",
                        phase="DEV",
                        iteration=state.dev_iteration,
                        reason="spec_gap",
                    )
        state.retry_reason = RetryReason.SPEC_GAP_RESUME
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result="DEV_SPEC_GAP",
        )
        _log(
            f"  ↻ DEV   specification gap resolved (iter={state.dev_iteration} → "
            f"re-entering dev; no review cycle spent)"
        )
        if logger:
            logger._safe_emit(
                "phase_end",
                phase="DEV",
                outcome="spec_gap_resume",
                iteration=state.dev_iteration,
            )
        return None

    # ── Unproven-completion guard (successful dev only) ──────────────
    # Fail closed at the dev seam: a dev that exits successfully but hands off a
    # completion claim (an acceptance criterion marked MET) without gate PASS
    # evidence has reported done without proving the gate ran. Escalate rather
    # than accept an unverified completion and waste a downstream gate run
    # rediscovering the failure. This is the coordinator catching what the dev
    # should have declared as a blocking failure (gate_result: BLOCKED).
    #
    # Exception: a review-fix / P2-cleanup iteration whose prompt delegated gate
    # execution to the coordinator (state.gate_delegated_this_iteration) is
    # *expected* to hand off MET-without-PASS — the prompt told the agent not to
    # re-run the gate. Escalating there would block the coordinator's own
    # authoritative VALIDATE gate from running on the latest fix commit (see
    # issue #1871). The delegation flag is set authoritatively by the coordinator
    # at prompt-routing time, not read from the agent's handoff, so an ordinary
    # iteration cannot bypass the guard by self-reporting `gate_delegated`
    # (honor_gate_delegation=False below ignores the handoff-level marker here).
    _claims_unproven = dev_result.success and dev_handoff_claims_unproven_completion(
        dev_result.dev_handoff or {}, honor_gate_delegation=False
    )
    if _claims_unproven and state.gate_delegated_this_iteration:
        _log_verbose(
            "  Dev handoff claims completion without self-reported gate PASS, but "
            "gate execution was delegated to the coordinator this iteration — "
            "proceeding to VALIDATE for the authoritative gate result."
        )
    elif _claims_unproven:
        state.phase = Phase.ESCALATE
        state.error = (
            "Dev handoff claims completion (acceptance criteria MET) without gate PASS "
            "evidence — the gate was not proven to pass; refusing to accept an "
            "unverified completion"
        )
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result="HANDOFF_NO_GATE_EVIDENCE",
        )
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("escalate", reason=state.error, phase="DEV")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    if not dev_result.success:
        if _runner_failure is not None:
            runner_name = _runner_display_name(config)
            state.phase = Phase.ESCALATE
            state.error_type = dev_result.failure_code
            state.error = (
                f"Runner crashed before agent execution: {runner_name}: {_runner_failure[1]}"
            )
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result="RUNNER_CRASH",
                runner_failure_summary=_runner_failure[1],
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("escalate", reason=state.error, phase="DEV")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )
        if dev_result.failure_code == "max_iterations_reached" and dev_result.dev_handoff is None:
            state.error_type = "max_iterations_no_submit"
            state.retry_reason = RetryReason.MAX_ITERATIONS_NO_SUBMIT
            if not state.dev_escalated:
                _old_model = config.dev_profile.model
                if config.retry.auto_model_escalation and config.models is not None:
                    # Registry passed as-is: an explicitly empty {} stays empty
                    # instead of collapsing to the built-in default (`... or None`).
                    _registry = config.model_registry
                    _curr_key = _find_registry_key_for_profile(
                        config.dev_profile, registry=_registry
                    )
                    if _curr_key is not None:
                        _next_key = _escalate_dev_model(
                            _curr_key, config.models, registry=_registry
                        )
                        if _next_key is not None:
                            from theforge.config.models import (  # noqa: PLC0415
                                _resolve_model_info,
                            )

                            _next_info = _resolve_model_info(_next_key, registry=_registry)
                            _new_dev = apply_model_info(config.dev_profile, _next_info)
                            config.dev_profile = _new_dev
                            state.dev_escalated = True
                            state.escalation_note = (
                                "MODEL ESCALATION: The previous dev iteration exhausted "
                                "its iteration budget without calling submit. "
                                f"Previous model: {_old_model}. "
                                f"Escalated model: {_next_info.model}."
                            )
                if not state.dev_escalated:
                    state.escalation_note = (
                        "RETRY ADAPTATION: The previous dev iteration exhausted its "
                        "iteration budget without calling submit. "
                        f"Previous model: {_old_model}. The retry uses explicit submit "
                        "pressure and narrower scope instead of repeating unchanged "
                        "conditions."
                    )
            _submit_pressure_feedback = (
                "The previous dev iteration exhausted its iteration budget without calling the "
                "submit tool, so there is no structured handoff to continue from. Do not repeat "
                "the same exploratory loop. Narrow scope, stabilize the worktree, and submit a "
                "structured result promptly."
            )
            state.human_feedback = _append_retry_guidance(
                state.human_feedback, _submit_pressure_feedback
            )
            # Preserve any partial edits before retrying (#1746). Like the
            # timeout resume below, this is another retry-with-possibly-dirty-
            # worktree case: the agent burned its internal iteration budget
            # without committing, so whatever it produced is uncommitted
            # working-tree state the next attempt would otherwise branch from as
            # if empty. Checkpoint it so the retry continues from committed work.
            if _worktree_has_changes(workspace_path):
                _checkpointed = _checkpoint_commit(
                    workspace_path, "max_iterations_reached without submit"
                )
                if _checkpointed:
                    _log(
                        "  ⎇ DEV   checkpoint-committed stranded work before max-iterations retry"
                    )
                    if logger:
                        logger._safe_emit(
                            "dev_checkpoint_commit",
                            phase="DEV",
                            iteration=state.dev_iteration,
                            reason="max_iterations_reached",
                        )
            return None

    if (
        dev_result.success
        and state.error_type != "max_iterations_no_submit"
        and state.total_dev_cost
        > (state.adaptive_dev_cost_estimate_usd or config.dev_profile.budget_usd)
    ):
        # The per-story dollar value is a historical-cost ESTIMATE, not an
        # enforced budget. Post-hoc dollar governance lives at the sprint level
        # (forge.yaml budget_usd); exceeding the per-story estimate is never, by
        # itself, an operator-actionable overrun. So there are only two outcomes:
        #   - committed work → the estimate was simply low; proceed, no action.
        #   - no commits → the attempt produced no usable output; escalate on the
        #     unproductive-attempt semantics (what actually went wrong), NOT a
        #     dollar overrun.
        _cost_estimate = state.adaptive_dev_cost_estimate_usd or config.dev_profile.budget_usd
        if _commits_exist_strict(workspace_path, config.workspace.base_branch):
            _log_verbose(
                f"  DEV cost ${state.total_dev_cost:.4f} exceeded the per-story estimate "
                f"${_cost_estimate:.4f} — committed work found; estimate was low, proceeding "
                "to validate/review (per-story estimates are not enforced budgets)"
            )
        else:
            state.phase = Phase.ESCALATE
            state.error = (
                f"Dev attempt produced no usable output "
                f"(${state.total_dev_cost:.4f} spent, no commits)"
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("escalate", reason=state.error, phase="DEV")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

    if not dev_result.success:
        _is_timeout = dev_result.failure_code == "timeout"
        if _is_timeout:
            # Record the wall-clock kill on state immediately, before any
            # retry/escalate/fall-through decision runs. This is the only
            # reliable signal that the dev process was harness-killed at its
            # timeout: the killed iteration's telemetry entry can be overwritten
            # by a later VALIDATE-phase write once checkpoint-committed work lets
            # execution fall through (#1754). model_profiles_bridge reads this to
            # segregate the run as a censored observation for the kill floor.
            state.dev_process_timeout_killed = True
        if dev_result.failure_code == "stuck_pattern":
            # Record the stuck-pattern terminate on state immediately, before any
            # retry/escalate/fall-through decision runs — same rationale as the
            # timeout flag above (#1754 lets execution fall through, overwriting
            # the terminated iteration's telemetry). model_profiles_bridge reads
            # this to segregate the run as a harness-imposed termination that must
            # not contaminate the model's capability statistics.
            state.dev_process_stuck_terminated = True
        # The signal number records only what was done to the process, not what
        # went wrong. When the runner already explained the failure in words
        # (e.g. "TIMEOUT: Agent exceeded 900s limit"), surface that instead of
        # the raw exit code so the operator is not left to reconstruct a fact
        # the system already had.
        _failure_detail = _describe_dev_failure(dev_result, is_timeout=_is_timeout)
        _log_verbose(f"Dev agent failed ({_failure_detail})")
        # ── Checkpoint-commit stranded work (#1746) ──────────────────────
        # A killed/failed dev iteration may leave correct work as uncommitted
        # working-tree state that every commit-reasoning mechanism below is
        # blind to: the timeout-retry (which would restart from an empty
        # base), the zero-commit guard (which would escalate on a diff that is
        # not actually empty), integration, and the audit trail. The agent may
        # already be gone (SIGKILL); the coordinator owns the worktree and is
        # the only party still alive able to commit. Preserve whatever was
        # produced as a checkpoint commit BEFORE any retry/escalate decision
        # runs. Committing only happens when the worktree is genuinely dirty,
        # so a truly empty iteration still escalates as before.
        if _worktree_has_changes(workspace_path):
            _checkpointed = _checkpoint_commit(workspace_path, _failure_detail)
            if _checkpointed:
                _log(
                    f"  ⎇ DEV   checkpoint-committed stranded work before "
                    f"failure handling ({_failure_detail})"
                )
                if logger:
                    logger._safe_emit(
                        "dev_checkpoint_commit",
                        phase="DEV",
                        iteration=state.dev_iteration,
                        reason=_failure_detail,
                    )
        # ── Provider quota exhausted with no applicable fallback (#2298) ─
        # The provider stated when its limit resets and no configured transport
        # fallback applied, so every remaining iteration would re-ask a provider
        # that has already answered. Stop here instead of spending the budget to
        # rediscover the same refusal. This is an infrastructure abort, not an
        # escalation: no model judged this story, so the run must not leave a
        # story-quality verdict — or router-teaching evidence — behind it.
        _quota_halt = _unrecoverable_provider_quota(dev_result)
        if _quota_halt is not None:
            _quota_failure = AgentInvocationFailure(
                phase="DEV",
                category=CATEGORY_TRANSPORT,
                exit_code=dev_result.exit_code,
                failure_code=dev_result.failure_code,
                detail=_quota_halt,
                profile_name=getattr(config.dev_profile, "name", None),
                extra={
                    "provider_quota_reset_at": dev_result.provider_quota_reset_at,
                    "transport_fallback_reason": dev_result.transport_fallback_reason,
                    "transport_fallback_not_applied_reason": (
                        dev_result.transport_fallback_not_applied_reason
                    ),
                },
            )
            record_invocation_failure(state, _quota_failure)
            state.phase = Phase.ESCALATE
            state.error = (
                f"Dev provider refused on quota and no transport fallback was "
                f"applicable ({_quota_halt}) — halting rather than spending the "
                f"remaining {state.budget.remaining()} iteration(s) against a "
                "provider that has stopped answering"
            )
            mark_infrastructure_abort(state, _quota_failure, message=state.error)
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result="DEV_PROVIDER_QUOTA_EXHAUSTED",
            )
            _log(f"✗ ABORT   {state.error}")
            if logger:
                logger._safe_emit(
                    "infrastructure_abort",
                    phase="DEV",
                    reason=state.error,
                    category=_quota_failure.category,
                    provider_quota_reset_at=dev_result.provider_quota_reset_at,
                    transport_fallback_not_applied_reason=(
                        dev_result.transport_fallback_not_applied_reason
                    ),
                )
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
                infrastructure_failure=True,
            )
        # ── Timeout retry (iterations remaining) ─────────────────────────
        # A per-iteration timeout is a retryable failure, not a terminal
        # escalation. Running out of time is not the same event as crashing or
        # producing wrong work: it is ordinarily retryable and arrives with its
        # own explanation. Where dev iterations remain, spend one and re-enter
        # dev with the timeout and its limit stated in context rather than
        # ending the story with unused budget. The empty-diff guard's job is to
        # keep a zero-commit run from reaching APPROVE — it must not also become
        # the thing that declares a story terminal while a safe outcome (another
        # attempt) remained available. #1216 established this for the gate; it
        # applies equally to the dev phase.
        if _is_timeout and not state.budget.is_exhausted():
            state.retry_reason = RetryReason.TIMEOUT_RESUME
            state.human_feedback = _append_retry_guidance(
                state.human_feedback,
                f"Your previous dev iteration was cut off by a timeout: {_failure_detail}. "
                "Any work you had already produced was checkpoint-committed for you, so "
                "the branch already contains it — continue from that committed state rather "
                "than redoing it. Narrow the remaining scope so you finish within the time "
                "limit.",
            )
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result="DEV_TIMEOUT",
                is_timeout=True,
            )
            _log(
                f"  ✗ DEV   TIMEOUT  (iter={state.dev_iteration} → retrying dev; "
                f"{state.budget.remaining()} iteration(s) remaining)"
            )
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="DEV",
                    outcome="timeout_retry",
                    iteration=state.dev_iteration,
                )
            return None
        # ── Zero-commit guard (any failed dev iteration) ─────────────────
        # If the dev agent exited with failure (non-zero or signal-killed) and
        # the worktree has no new commits ahead of base, escalate immediately
        # rather than letting an empty diff flow through to a fake APPROVE.
        _has_branch_commits = _has_commits_ahead_of_base(
            workspace_path, config.workspace.base_branch
        )
        _changed_since_start = _worktree_changed_since_commit(
            workspace_path, state.last_dev_start_commit
        )
        # ── Infrastructure abort vs. genuine escalation (#1951) ──────
        # Refusing to APPROVE an empty diff is right either way. What differs
        # is what the run is entitled to CLAIM about the story. ESCALATE is the
        # outcome reserved for a story whose framing an agent found invalid —
        # it asserts a judgment. When the dev invocation produced no model
        # output at all (credential rejected, transport dropped, process never
        # started), no agent judged anything, and recording ESCALATE writes a
        # story-quality verdict that no model ever formed — one that then
        # outlives the run in escalation memory.
        _invocation_failure = classify_agent_failure(
            dev_result,
            phase="DEV",
            profile_name=getattr(config.dev_profile, "name", None),
            detail=_failure_detail,
        )
        _left_no_observable_work = (not _has_branch_commits) or (
            _invocation_failure is not None and _changed_since_start is False
        )
        if _left_no_observable_work:
            if _invocation_failure is not None:
                _failure_extra = {
                    **_invocation_failure.extra,
                    **_pending_dev_transport_retry_failure_extra(state),
                }
                if _failure_extra != _invocation_failure.extra:
                    _invocation_failure = _dc_replace(_invocation_failure, extra=_failure_extra)
                record_invocation_failure(state, _invocation_failure)
                _unused_dev_iteration = zero_charge_no_model_artifacts(dev_result)
                if _unused_dev_iteration:
                    _rollback_recorded_dev_attempt(
                        state,
                        dev_results_len=_dev_results_before,
                        dev_durations_len=_dev_durations_before,
                        dev_handoff_len=_dev_handoff_before,
                    )
                state.phase = Phase.ESCALATE
                # Name the failure the way the phase already names it
                # (_failure_detail states a timeout and its limit rather than the
                # signal number, per #1216) and add the substrate category.
                _work_clause = (
                    "and no commits ahead of base"
                    if not _has_branch_commits
                    else "and left the preserved branch unchanged"
                )
                state.error = (
                    f"Dev agent produced no model output "
                    f"(category={_invocation_failure.category}: {_failure_detail}) {_work_clause} "
                    "— aborting as an infrastructure failure; "
                    "no judgment was obtained about this story"
                )
                mark_infrastructure_abort(state, _invocation_failure, message=state.error)
                if not _unused_dev_iteration:
                    record_dev_iteration_telemetry(
                        state,
                        workspace_path,
                        max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                        gate_result="DEV_INFRA_FAILURE",
                    )
                _log(f"✗ ABORT   infrastructure failure: {state.error}")
                if logger:
                    logger._safe_emit(
                        "infrastructure_abort",
                        phase="DEV",
                        reason=state.error,
                        category=_invocation_failure.category,
                    )
                # No escalation notification: nothing was learned about the
                # story, so there is nothing for a human to adjudicate about it.
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                    infrastructure_failure=True,
                    unused_dev_iteration=_unused_dev_iteration,
                )
            state.phase = Phase.ESCALATE
            _work_clause = (
                "produced no commits ahead of base"
                if not _has_branch_commits
                else "left the preserved branch unchanged"
            )
            state.error = (
                f"Dev agent failed ({_failure_detail}) and {_work_clause} "
                "— escalating to avoid an empty-diff APPROVE"
            )
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result="DEV_FAILURE",
                # What the agent last said, recorded as its own field rather than
                # left only inside the sentence built around it (#2427). Sprint
                # RCA quotes it, so the operator reads the run's stated ending
                # without opening the dev-iteration log.
                runner_failure_summary=_captured_agent_text(dev_result),
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("escalate", reason=state.error, phase="DEV")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

    # ── Zero-change guard (review-driven retry only) ─────────────────
    # If the coordinator retried DEV after review REQUEST_CHANGES and the dev
    # agent produced no changes relative to the previous iteration baseline,
    # escalate immediately. This rejects self-reported handoffs when the
    # worktree is unchanged instead of burning another review cycle.
    # Only applies when THIS dev pass was entered for review_changes or extend —
    # gate retries and timeout resumes may legitimately produce no code changes.
    _is_review_driven = _dev_entry_reason in ("review_changes", "extend")
    if _is_review_driven and state.last_dev_start_commit:
        _has_commits = False
        _has_dirty = False
        try:
            _diff_proc = subprocess.run(
                ["git", "diff", "--quiet", state.last_dev_start_commit, "HEAD"],
                cwd=str(workspace_path),
                capture_output=True,
                timeout=10,
            )
            _has_commits = _diff_proc.returncode != 0  # exit 1 = diff exists
        except Exception:  # noqa: BLE001
            pass
        try:
            _status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(workspace_path),
                capture_output=True,
                timeout=10,
            )
            _has_dirty = bool(_status_proc.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
        if not _has_commits and not _has_dirty:
            state.phase = Phase.ESCALATE
            state.error = (
                "Dev retry produced no changes in the worktree relative to the previous "
                "iteration baseline — escalating to avoid re-reviewing identical code"
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("escalate", reason=state.error, phase="DEV")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

    # ── Worktree git-state consistency boundary (advance path only) ──────
    # A dev iteration has no legitimate need to mutate the branch state of its
    # worktree. Residue from a partially applied operation (in-progress
    # rebase/merge/cherry-pick/revert/bisect), a clean-but-illegitimate HEAD/ref
    # change (reset --hard / force-push behind the pre-dev base), a
    # dev-introduced merge commit, or a checkout onto the wrong branch is
    # corrupted state that must never flow silently into review or integration.
    # Establish the boundary invariant here — fail-closed on residue, attributed
    # to DEV — before advancing (#1365). Only runs on the successful-dev
    # fall-through; the timeout/max-iterations retry returns above re-enter DEV.
    #
    # expected_branch_name=branch_name enforces the spec's "refs matching what
    # the coordinator expects" clause: a coordinator worktree is created on the
    # story branch (`git worktree add <path> <branch>`) and every rebase keeps
    # HEAD on it, so a dev iteration that ends detached — or checked out onto a
    # different ref — has mutated branch state that integration would silently
    # skip (it force-pushes the *named* branch, not the detached commit). A
    # detached HEAD is treated as corrupt here exactly as workspace.py already
    # treats it during worktree reuse. The branch check fails open on a git
    # error (e.g. a non-repo scratch dir), so only a genuine wrong-ref state
    # escalates.
    _wt_state = check_worktree_git_consistency(
        workspace_path,
        expected_base_sha=state.last_dev_start_commit,
        base_branch=config.workspace.base_branch,
        expected_branch_name=branch_name,
    )
    if not _wt_state.consistent:
        state.phase = Phase.ESCALATE
        _wt_detail = f" ({_wt_state.detail})" if _wt_state.detail else ""
        state.error = (
            f"DEV phase left the worktree in an inconsistent git state "
            f"({_wt_state.inconsistency}){_wt_detail} — refusing to advance to "
            "review/integration"
        )
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result="WORKTREE_STATE_INCONSISTENT",
        )
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("escalate", reason=state.error, phase="DEV")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    return None
