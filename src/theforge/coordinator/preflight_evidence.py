"""Partial-evidence artifact for failed preflight runs (issue #706).

When the preflight agent crashes, times out, or is killed mid-exploration, the
work it already did — the files it inspected and the tool calls it made — is
real, paid-for signal. This module defines the artifact that preserves that
exploration so the PLAN phase can consume it instead of starting from scratch.

The artifact is intentionally provider-neutral and pure-data (stdlib only): it
is built from an ``AgentResult``'s best-effort ``tool_trace`` / ``partial_output``
fields, serialized into the audit trail alongside the existing preflight
failure fields (``degraded`` / ``degraded_reason`` / ``failure_action``), and
rendered into a compact markdown block for the plan prompt.

Artifact contract — see also ``docs/reference/preflight-partial-evidence.md``::

    files_inspected:  tuple[str, ...]   # unique file paths the agent read/edited
    tool_calls:       tuple[dict, ...]  # ordered [{"tool": str, "target": str|None}]
    partial_conclusion: str | None      # last substantive agent text, if captured
    failure_reason:   str | None        # the failure marker on result.output
    failure_code:     str | None        # stable machine identifier (timeout, ...)
    exit_code:        int | None
    cost_usd:         float | None
    duration_s:       float | None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tools whose target argument names a file the agent actually inspected or
# touched. Search tools (Grep/Glob) are excluded from files_inspected because
# their target is a pattern/directory, not a concrete file read — they still
# appear in tool_calls.
_FILE_INSPECTION_TOOLS = frozenset(
    {"Read", "Edit", "Write", "NotebookEdit", "MultiEdit", "View", "Cat"}
)

# Bound the free-text fields so a runaway agent transcript cannot bloat the
# audit record or the plan prompt.
_MAX_CONCLUSION_CHARS = 4000
_MAX_REASON_CHARS = 1000
_MAX_RENDERED_FILES = 40
_MAX_RENDERED_CALLS = 60


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class PreflightPartialEvidence:
    """Exploration salvaged from a failed preflight run.

    ``is_empty()`` is True when the agent died before doing any observable
    work AND left no partial conclusion — in that case there is nothing worth
    surfacing to the plan phase.
    """

    files_inspected: tuple[str, ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()
    partial_conclusion: str | None = None
    failure_reason: str | None = None
    failure_code: str | None = None
    exit_code: int | None = None
    cost_usd: float | None = None
    duration_s: float | None = None

    def is_empty(self) -> bool:
        return not (self.files_inspected or self.tool_calls or self.partial_conclusion)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for the audit trail / YAML artifact."""
        return {
            "files_inspected": list(self.files_inspected),
            "tool_calls": [dict(call) for call in self.tool_calls],
            "partial_conclusion": self.partial_conclusion,
            "failure_reason": self.failure_reason,
            "failure_code": self.failure_code,
            "exit_code": self.exit_code,
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
        }

    def render_for_plan(self) -> str:
        """Render a compact markdown block for injection into the plan prompt.

        Returns "" when empty so callers can treat it as an optional section.
        """
        if self.is_empty():
            return ""
        lines: list[str] = [
            "A previous preflight run failed before finishing, but it had "
            "already explored the codebase. Use this to avoid re-reading the "
            "same files — verify anything you rely on, as the exploration is "
            "incomplete.",
        ]
        if self.failure_code or self.failure_reason:
            marker = self.failure_code or (self.failure_reason or "").strip()
            lines.append(f"\nFailure: {marker}")
        if self.files_inspected:
            shown = list(self.files_inspected[:_MAX_RENDERED_FILES])
            lines.append("\nFiles already inspected:")
            lines.extend(f"- {path}" for path in shown)
            extra = len(self.files_inspected) - len(shown)
            if extra > 0:
                lines.append(f"- … and {extra} more")
        if self.tool_calls:
            shown_calls = list(self.tool_calls[:_MAX_RENDERED_CALLS])
            lines.append("\nTool calls made:")
            for call in shown_calls:
                target = call.get("target")
                suffix = f" {target}" if target else ""
                lines.append(f"- {call.get('tool', '?')}{suffix}")
            extra_calls = len(self.tool_calls) - len(shown_calls)
            if extra_calls > 0:
                lines.append(f"- … and {extra_calls} more")
        if self.partial_conclusion:
            lines.append("\nPartial conclusion reached before the crash:")
            lines.append(self.partial_conclusion)
        return "\n".join(lines)


def build_partial_evidence(
    result: Any,
    *,
    duration_s: float | None = None,
) -> PreflightPartialEvidence:
    """Build a :class:`PreflightPartialEvidence` from a failed ``AgentResult``.

    Duck-typed on ``result`` so this module stays stdlib-only and decoupled
    from the runners package. Reads ``tool_trace`` and ``partial_output`` when
    present (Claude streaming path); degrades to failure metadata only for
    providers that do not stream tool activity.
    """
    raw_trace = getattr(result, "tool_trace", ()) or ()
    tool_calls: list[dict[str, Any]] = []
    files: list[str] = []
    seen_files: set[str] = set()
    for entry in raw_trace:
        if not isinstance(entry, dict):
            continue
        tool = str(entry.get("tool", "?"))
        target = entry.get("target")
        target = target if isinstance(target, str) and target.strip() else None
        tool_calls.append({"tool": tool, "target": target})
        if tool in _FILE_INSPECTION_TOOLS and target and target not in seen_files:
            seen_files.add(target)
            files.append(target)

    partial_conclusion = _truncate(getattr(result, "partial_output", None), _MAX_CONCLUSION_CHARS)
    failure_reason = _truncate(getattr(result, "output", None), _MAX_REASON_CHARS)

    return PreflightPartialEvidence(
        files_inspected=tuple(files),
        tool_calls=tuple(tool_calls),
        partial_conclusion=partial_conclusion,
        failure_reason=failure_reason,
        failure_code=getattr(result, "failure_code", None),
        exit_code=getattr(result, "exit_code", None),
        cost_usd=getattr(result, "cost_usd", None),
        duration_s=duration_s,
    )


def render_partial_evidence(evidence: dict[str, Any] | None) -> str | None:
    """Render a stored partial-evidence dict into a plan-prompt markdown block.

    Accepts the ``to_dict()`` form persisted on ``CoordinatorState`` /
    the audit trail. Returns None when there is nothing worth surfacing so the
    plan call site can pass through cleanly.
    """
    if not evidence:
        return None
    reconstructed = PreflightPartialEvidence(
        files_inspected=tuple(evidence.get("files_inspected") or ()),
        tool_calls=tuple(evidence.get("tool_calls") or ()),
        partial_conclusion=evidence.get("partial_conclusion"),
        failure_reason=evidence.get("failure_reason"),
        failure_code=evidence.get("failure_code"),
        exit_code=evidence.get("exit_code"),
        cost_usd=evidence.get("cost_usd"),
        duration_s=evidence.get("duration_s"),
    )
    rendered = reconstructed.render_for_plan()
    return rendered or None
