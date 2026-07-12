"""Progress-aware stuck-agent detection shared by API and CLI runners.

Tracks three observable patterns across loop iterations:
  - "repeat": identical (tool name + arguments) signatures repeated
  - "error_loop": identical error tool-result content repeated
  - "no_progress": consecutive iterations with no successful file modification
    — TELEMETRY ONLY. This signal is too imprecise to terminate on (it cannot
    distinguish a real loop from legitimate exploration), so it never fires a
    nudge or terminate. The counter is recorded in the audit so operators can
    still observe long no-modification stretches without the system killing
    work it should not.

On detection of a defensible pattern (repeat / error_loop) the tracker emits a
one-shot nudge for the active pattern KIND. If the SAME kind persists for
``post_nudge_iterations`` more iterations, the tracker emits a termination
reason with structured evidence (per-iteration tool-call fingerprints, the
iteration where the loop began, and the supporting counter value). If the
pattern changes or breaks after a nudge, the post-nudge counter resets and the
tracker re-arms so a fresh incident can be nudged again later.

The tracker is gated by ``profile.phase == "dev"`` so review/preflight loops
remain unaffected. The runner translates raw turn data into an
``IterationObservation`` once per agent iteration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from theforge.config import ModelProfile

PATTERN_REPEAT = "repeat"
PATTERN_NO_PROGRESS = "no_progress"  # telemetry only — never returned as active pattern
PATTERN_ERROR_LOOP = "error_loop"

# Modify-capable tool names (CLI form and API-registry form).
MODIFY_TOOL_NAMES: frozenset[str] = frozenset({"write_file", "edit_file", "Write", "Edit"})

# Read/search/navigation tool names — distinct calls in this set count as
# exploration progress for telemetry purposes.
EXPLORATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"read_file", "Read", "glob", "Glob", "grep", "Grep", "list_dir", "ls"}
)

# Argument names whose values may carry large or sensitive content; redacted
# in the audit fingerprint of every tool call so the trace stays compact and
# does not leak file bodies or edit payloads.
_SENSITIVE_ARG_NAMES: frozenset[str] = frozenset(
    {"content", "new_content", "old_string", "new_string", "input"}
)

# Cap the number of history entries kept per run; recent iterations are what
# matter for diagnosing a loop, and unbounded growth would bloat the audit.
_MAX_HISTORY_ENTRIES = 50

# Cap the per-iteration tool-call list emitted in evidence so a runaway turn
# (model emits 100 tool calls in one go) doesn't dominate the audit reason.
_MAX_CALLS_PER_ITER_IN_EVIDENCE = 8


def make_signature(name: str | None, arguments: dict | None) -> str:
    """Stable (name, args) signature used to detect repeated identical calls."""
    try:
        args_str = json.dumps(arguments or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args_str = repr(arguments)
    return f"{name or ''}|{args_str}"


def _redact_arguments(arguments: dict | None) -> dict:
    """Return a shallow copy of *arguments* with sensitive values masked."""
    if not isinstance(arguments, dict):
        return {}
    return {k: "<redacted>" if k in _SENSITIVE_ARG_NAMES else v for k, v in arguments.items()}


def _fingerprint(name: str | None, arguments: dict | None) -> str:
    """Compact human-readable per-call fingerprint for the audit trail."""
    try:
        args_str = json.dumps(_redact_arguments(arguments), sort_keys=True, default=str)
    except (TypeError, ValueError):
        args_str = repr(arguments)
    if len(args_str) > 200:
        args_str = args_str[:197] + "..."
    return f"{name or ''}({args_str})"


@dataclass(frozen=True)
class IterationObservation:
    """One agent iteration distilled into the inputs the tracker needs.

    ``signatures`` is the set of (name, args) signatures of every well-formed
    tool call the agent issued this iteration. ``successful_modify`` is True
    iff at least one Write/Edit call returned a non-error result — failed
    write attempts do not count as progress. ``error_content`` is the first
    error tool-result content from this iteration (truncated), used to detect
    repeated identical errors across iterations. ``call_fingerprints`` is the
    redacted per-call fingerprint list used for audit only. ``exploration_sigs``
    is the subset of signatures whose tool name is in EXPLORATION_TOOL_NAMES.
    """

    signatures: frozenset[str]
    successful_modify: bool
    error_content: str | None
    call_fingerprints: tuple[str, ...] = ()
    exploration_sigs: frozenset[str] = field(default_factory=frozenset)


def build_observation(
    calls: list,
    results: list[dict],
) -> IterationObservation:
    """Build an IterationObservation from a list of tool-call requests and result dicts.

    ``calls`` is a list of ToolCallRequest-like objects (need ``name``/``arguments``).
    ``results`` is the dict list produced by the API runner: each entry has
    ``id``, ``name``, ``content``.
    """
    sigs: set[str] = set()
    exploration_sigs: set[str] = set()
    fingerprints: list[str] = []
    for c in calls:
        name = getattr(c, "name", None)
        args = getattr(c, "arguments", None)
        if name and isinstance(args, dict):
            sig = make_signature(name, args)
            sigs.add(sig)
            if name in EXPLORATION_TOOL_NAMES:
                exploration_sigs.add(sig)
            fingerprints.append(_fingerprint(name, args))

    successful_modify = False
    error_content: str | None = None
    for r in results:
        name = r.get("name", "")
        content = str(r.get("content", ""))
        is_error = content.startswith("Error")
        if name in MODIFY_TOOL_NAMES and not is_error:
            successful_modify = True
        if error_content is None and is_error:
            error_content = content[:200]

    return IterationObservation(
        signatures=frozenset(sigs),
        successful_modify=successful_modify,
        error_content=error_content,
        call_fingerprints=tuple(fingerprints),
        exploration_sigs=frozenset(exploration_sigs),
    )


class StuckTracker:
    """Per-run state machine for stuck-agent pattern detection.

    Call ``observe`` once per agent iteration with an IterationObservation.
    Returns ``(nudge_msg, terminate_reason, pattern_text)`` — at most one of
    nudge_msg/terminate_reason is non-None per call. ``pattern_text`` is the
    human-readable description of the active pattern when either fires.
    """

    def __init__(self, profile: ModelProfile) -> None:
        cfg = profile.stuck_detection
        self._enabled = bool(cfg and cfg.enabled and profile.phase == "dev")
        self._cfg = cfg
        self._has_modify_tools = bool(
            profile.allowed_tools and any(t in profile.allowed_tools for t in MODIFY_TOOL_NAMES)
        )
        self._iteration = 0
        self._last_sig: frozenset[str] | None = None
        self._repeat_count = 0
        self._repeat_start_iter: int | None = None
        self._iters_without_mod = 0  # telemetry only — does not trigger nudge/terminate
        self._distinct_exploration_sigs: set[str] = set()
        self._exploration_progress_iters = 0  # telemetry: iters that added a new exploration sig
        self._last_error: str | None = None
        self._error_count = 0
        self._error_start_iter: int | None = None
        self._nudge_sent = False
        self._nudge_kind: str | None = None
        self._post_nudge_iters = 0
        # Bounded ring of recent per-iteration records, used as audit evidence
        # when a termination fires. Each entry: dict with iteration index and
        # the redacted call fingerprints that ran in that iteration.
        self._history: list[dict] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def nudge_kind(self) -> str | None:
        return self._nudge_kind

    @property
    def iters_without_modification(self) -> int:
        """Telemetry: consecutive iterations with no successful file modification."""
        return self._iters_without_mod

    @property
    def distinct_exploration_count(self) -> int:
        """Telemetry: distinct (name, args) read/search calls seen this run."""
        return len(self._distinct_exploration_sigs)

    @property
    def exploration_progress_iters(self) -> int:
        """Telemetry: number of iterations that added a new distinct exploration sig."""
        return self._exploration_progress_iters

    def observe(self, obs: IterationObservation) -> tuple[str | None, str | None, str | None]:
        if not self._enabled or self._cfg is None:
            return (None, None, None)

        self._iteration += 1
        sig = obs.signatures

        # Repeat detection: identical signature set across consecutive iterations.
        if self._last_sig is not None and sig == self._last_sig and sig:
            if self._repeat_count == 0:
                self._repeat_start_iter = self._iteration - 1
            self._repeat_count += 1
        else:
            self._repeat_count = 1 if sig else 0
            self._repeat_start_iter = self._iteration if sig else None
        self._last_sig = sig

        # No-progress arm (telemetry only): only a *successful* modify call
        # counts as progress. Iterations with no tool calls at all are also
        # progress-neutral.
        if obs.successful_modify or not sig:
            self._iters_without_mod = 0
        else:
            self._iters_without_mod += 1

        # Exploration-progress telemetry: distinct read/search calls across
        # iterations are real work even when no edit landed.
        new_exploration = obs.exploration_sigs - self._distinct_exploration_sigs
        if new_exploration:
            self._exploration_progress_iters += 1
            self._distinct_exploration_sigs.update(new_exploration)

        cur_error = obs.error_content
        if cur_error and cur_error == self._last_error:
            if self._error_count <= 1:
                self._error_start_iter = self._iteration - 1
            self._error_count += 1
        else:
            self._error_count = 1 if cur_error else 0
            self._error_start_iter = self._iteration if cur_error else None
        self._last_error = cur_error

        self._record_history(obs)

        active_kind, active_text = self._active_pattern()

        if not self._nudge_sent:
            if active_kind is not None and active_text is not None:
                self._nudge_sent = True
                self._nudge_kind = active_kind
                self._post_nudge_iters = 0
                return (self._build_nudge(active_text), None, active_text)
            return (None, None, None)

        # Already nudged: only count post-nudge iterations when the SAME kind
        # is still active. If the pattern broke or switched to a different
        # kind, reset and re-arm so a future incident can be nudged again.
        if active_kind is not None and active_kind == self._nudge_kind:
            self._post_nudge_iters += 1
            if self._post_nudge_iters >= self._cfg.post_nudge_iterations:
                assert active_text is not None
                return (None, self._build_terminate_reason(active_kind, active_text), active_text)
            return (None, None, None)

        # Pattern broke or changed kind — re-arm.
        self._post_nudge_iters = 0
        self._nudge_sent = False
        self._nudge_kind = None
        return (None, None, None)

    def _record_history(self, obs: IterationObservation) -> None:
        entry = {
            "iteration": self._iteration,
            "calls": list(obs.call_fingerprints[:_MAX_CALLS_PER_ITER_IN_EVIDENCE]),
            "successful_modify": obs.successful_modify,
            "had_error": obs.error_content is not None,
        }
        self._history.append(entry)
        if len(self._history) > _MAX_HISTORY_ENTRIES:
            del self._history[: len(self._history) - _MAX_HISTORY_ENTRIES]

    def _active_pattern(self) -> tuple[str | None, str | None]:
        if self._cfg is None:
            return (None, None)
        if self._repeat_count >= self._cfg.repeat_threshold and self._last_sig:
            preview = next(iter(self._last_sig))[:120]
            return (
                PATTERN_REPEAT,
                f"repeated identical tool calls ({self._repeat_count}x): {preview}",
            )
        if self._error_count >= self._cfg.error_threshold and self._last_error:
            return (
                PATTERN_ERROR_LOOP,
                f"error loop ({self._error_count}x): {self._last_error[:120]}",
            )
        # PATTERN_NO_PROGRESS is intentionally NOT returned: the
        # no-modification heuristic is too imprecise to terminate on. It is
        # tracked in self._iters_without_mod purely for telemetry.
        return (None, None)

    @staticmethod
    def _build_nudge(pattern: str) -> str:
        return (
            "[SYSTEM] Progress check: you appear to be stuck — "
            f"{pattern}. Try a different approach, gather more information with a "
            "different tool, or submit your current best result. Continuing the same "
            "behavior will cause this run to be terminated."
        )

    def _build_terminate_reason(self, kind: str, pattern: str) -> str:
        assert self._cfg is not None
        loop_start = self._repeat_start_iter if kind == PATTERN_REPEAT else self._error_start_iter
        evidence_lines = [
            f"stuck pattern persisted for {self._post_nudge_iters} iterations after nudge: "
            f"{pattern}",
            f"  signal: {kind}",
        ]
        if loop_start is not None:
            evidence_lines.append(f"  loop began at iteration {loop_start} (see dev log)")
        evidence_lines.append(f"  iterations observed: {self._iteration}")
        evidence_lines.append(
            "  no-modification streak (telemetry, not termination cause): "
            f"{self._iters_without_mod}"
        )
        recent = self._history[-min(8, self._iteration) :]
        if recent:
            evidence_lines.append("  recent iteration tool calls:")
            for entry in recent:
                calls_str = "; ".join(entry["calls"]) if entry["calls"] else "<no tool calls>"
                evidence_lines.append(f"    iter {entry['iteration']}: {calls_str}")
        return "\n".join(evidence_lines)

    def evidence(self) -> dict:
        """Structured snapshot of detector state for the audit trail.

        Always safe to call. Returns a dict with the bounded iteration history
        (redacted call fingerprints), counter values, and the loop-start
        pointer if a defensible pattern is currently active.
        """
        loop_start: int | None = None
        active_kind: str | None = None
        if self._cfg is not None:
            if self._repeat_count >= self._cfg.repeat_threshold:
                loop_start = self._repeat_start_iter
                active_kind = PATTERN_REPEAT
            elif self._error_count >= self._cfg.error_threshold:
                loop_start = self._error_start_iter
                active_kind = PATTERN_ERROR_LOOP
        return {
            "iterations_observed": self._iteration,
            "iters_without_modification": self._iters_without_mod,
            "distinct_exploration_calls": len(self._distinct_exploration_sigs),
            "exploration_progress_iters": self._exploration_progress_iters,
            "repeat_count": self._repeat_count,
            "error_count": self._error_count,
            "active_kind": active_kind,
            "loop_start_iteration": loop_start,
            "history": list(self._history),
        }
