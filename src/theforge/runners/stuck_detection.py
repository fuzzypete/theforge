"""Progress-aware stuck-agent detection shared by API and CLI runners.

Tracks three observable patterns across loop iterations:
  - "repeat": identical (tool name + arguments) signatures repeated
  - "no_progress": consecutive iterations with no successful file modification
  - "error_loop": identical error tool-result content repeated

On detection the tracker emits a one-shot nudge for the active pattern KIND.
If the SAME kind persists for ``post_nudge_iterations`` more iterations, the
tracker emits a termination reason. If the pattern changes or breaks after a
nudge, the post-nudge counter resets and the tracker re-arms so a fresh
incident can be nudged again later.

The tracker is gated by ``profile.phase == "dev"`` so review/preflight loops
remain unaffected. The runner translates raw turn data into an
``IterationObservation`` once per agent iteration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from theforge.config import ModelProfile

PATTERN_REPEAT = "repeat"
PATTERN_NO_PROGRESS = "no_progress"
PATTERN_ERROR_LOOP = "error_loop"

# Modify-capable tool names (CLI form and API-registry form).
MODIFY_TOOL_NAMES: frozenset[str] = frozenset({"write_file", "edit_file", "Write", "Edit"})


def make_signature(name: str | None, arguments: dict | None) -> str:
    """Stable (name, args) signature used to detect repeated identical calls."""
    try:
        args_str = json.dumps(arguments or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args_str = repr(arguments)
    return f"{name or ''}|{args_str}"


@dataclass(frozen=True)
class IterationObservation:
    """One agent iteration distilled into the inputs the tracker needs.

    ``signatures`` is the set of (name, args) signatures of every well-formed
    tool call the agent issued this iteration. ``successful_modify`` is True
    iff at least one Write/Edit call returned a non-error result — failed
    write attempts do not count as progress. ``error_content`` is the first
    error tool-result content from this iteration (truncated), used to detect
    repeated identical errors across iterations.
    """

    signatures: frozenset[str]
    successful_modify: bool
    error_content: str | None


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
    for c in calls:
        name = getattr(c, "name", None)
        args = getattr(c, "arguments", None)
        if name and isinstance(args, dict):
            sigs.add(make_signature(name, args))

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
        self._last_sig: frozenset[str] | None = None
        self._repeat_count = 0
        self._iters_without_mod = 0
        self._last_error: str | None = None
        self._error_count = 0
        self._nudge_sent = False
        self._nudge_kind: str | None = None
        self._post_nudge_iters = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def nudge_kind(self) -> str | None:
        return self._nudge_kind

    def observe(self, obs: IterationObservation) -> tuple[str | None, str | None, str | None]:
        if not self._enabled or self._cfg is None:
            return (None, None, None)

        sig = obs.signatures
        if self._last_sig is not None and sig == self._last_sig and sig:
            self._repeat_count += 1
        else:
            self._repeat_count = 1 if sig else 0
        self._last_sig = sig

        # No-progress arm: only a *successful* modify call counts as progress.
        # Iterations with no tool calls at all are also progress-neutral.
        if obs.successful_modify or not sig:
            self._iters_without_mod = 0
        else:
            self._iters_without_mod += 1

        cur_error = obs.error_content
        if cur_error and cur_error == self._last_error:
            self._error_count += 1
        else:
            self._error_count = 1 if cur_error else 0
        self._last_error = cur_error

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
                return (None, self._build_terminate_reason(active_text), active_text)
            return (None, None, None)

        # Pattern broke or changed kind — re-arm.
        self._post_nudge_iters = 0
        self._nudge_sent = False
        self._nudge_kind = None
        return (None, None, None)

    def _active_pattern(self) -> tuple[str | None, str | None]:
        if self._cfg is None:
            return (None, None)
        if self._repeat_count >= self._cfg.repeat_threshold and self._last_sig:
            preview = next(iter(self._last_sig))[:120]
            return (
                PATTERN_REPEAT,
                f"repeated identical tool calls ({self._repeat_count}x): {preview}",
            )
        if self._has_modify_tools and self._iters_without_mod >= self._cfg.no_progress_iterations:
            return (
                PATTERN_NO_PROGRESS,
                f"no file modifications over {self._iters_without_mod} consecutive iterations",
            )
        if self._error_count >= self._cfg.error_threshold and self._last_error:
            return (
                PATTERN_ERROR_LOOP,
                f"error loop ({self._error_count}x): {self._last_error[:120]}",
            )
        return (None, None)

    @staticmethod
    def _build_nudge(pattern: str) -> str:
        return (
            "[SYSTEM] Progress check: you appear to be stuck — "
            f"{pattern}. Try a different approach, gather more information with a "
            "different tool, or submit your current best result. Continuing the same "
            "behavior will cause this run to be terminated."
        )

    def _build_terminate_reason(self, pattern: str) -> str:
        assert self._cfg is not None
        return (
            f"stuck pattern persisted for {self._post_nudge_iters} iterations after nudge: "
            f"{pattern}"
        )
