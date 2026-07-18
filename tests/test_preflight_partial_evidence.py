"""Unit tests for the preflight partial-evidence artifact (issue #706).

Covers three layers:
  - runner_claude.extract_tool_trace: parsing tool calls from stream-json lines
  - preflight_evidence.build_partial_evidence: AgentResult -> artifact
  - preflight_evidence.render_partial_evidence: stored dict -> plan-prompt block

None of these exercise a real provider CLI — extract_tool_trace is a pure parse
of accumulated JSONL strings, and the evidence builder is duck-typed on a stub
result — so the suite runs with or without provider SDKs installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from theforge.coordinator.preflight_evidence import (
    PreflightPartialEvidence,
    build_partial_evidence,
    render_partial_evidence,
)
from theforge.runners.runner_claude import extract_tool_trace


def _assistant_line(*tool_uses: tuple[str, dict]) -> str:
    content = [{"type": "tool_use", "name": name, "input": inp} for name, inp in tool_uses]
    return json.dumps({"type": "assistant", "message": {"content": content}})


@dataclass
class _StubResult:
    """Duck-typed stand-in for AgentResult (build_partial_evidence uses getattr)."""

    tool_trace: tuple = ()
    partial_output: str | None = None
    output: str = ""
    failure_code: str | None = None
    exit_code: int | None = None
    cost_usd: float | None = None


# ── extract_tool_trace ───────────────────────────────────────────────────────


class TestExtractToolTrace:
    def test_extracts_tool_name_and_file_target(self):
        lines = [
            _assistant_line(("Read", {"file_path": "src/a.py"})),
            _assistant_line(("Grep", {"pattern": "def foo", "path": "src"})),
        ]
        trace = extract_tool_trace(lines)
        assert trace == (
            {"tool": "Read", "target": "src/a.py"},
            {"tool": "Grep", "target": "src"},
        )

    def test_pattern_target_when_no_path(self):
        lines = [_assistant_line(("Glob", {"pattern": "**/*.py"}))]
        assert extract_tool_trace(lines) == ({"tool": "Glob", "target": "**/*.py"},)

    def test_multiple_tool_uses_in_one_event_preserve_order(self):
        lines = [
            _assistant_line(
                ("Read", {"file_path": "a.py"}),
                ("Read", {"file_path": "b.py"}),
            )
        ]
        trace = extract_tool_trace(lines)
        assert [t["target"] for t in trace] == ["a.py", "b.py"]

    def test_target_none_when_no_recognized_arg(self):
        lines = [_assistant_line(("Bash", {"command": "ls -la"}))]
        assert extract_tool_trace(lines) == ({"tool": "Bash", "target": None},)

    def test_ignores_non_assistant_and_malformed_lines(self):
        lines = [
            "not json at all",
            json.dumps({"type": "result", "result": "done"}),
            "",
            _assistant_line(("Read", {"file_path": "src/keep.py"})),
        ]
        assert extract_tool_trace(lines) == ({"tool": "Read", "target": "src/keep.py"},)

    def test_empty_lines_yield_empty_trace(self):
        assert extract_tool_trace([]) == ()


# ── build_partial_evidence ───────────────────────────────────────────────────


class TestBuildPartialEvidence:
    def test_derives_unique_files_inspected_from_file_tools_only(self):
        result = _StubResult(
            tool_trace=(
                {"tool": "Read", "target": "src/a.py"},
                {"tool": "Read", "target": "src/a.py"},  # duplicate
                {"tool": "Edit", "target": "src/b.py"},
                {"tool": "Grep", "target": "src"},  # search tool excluded
            ),
            output="TIMEOUT: Agent exceeded 300s limit",
            failure_code="timeout",
            exit_code=-9,
            cost_usd=2.54,
        )
        ev = build_partial_evidence(result, duration_s=301.2)
        assert ev.files_inspected == ("src/a.py", "src/b.py")
        # all calls (including the search) are retained in tool_calls
        assert len(ev.tool_calls) == 4
        assert ev.failure_code == "timeout"
        assert ev.exit_code == -9
        assert ev.cost_usd == 2.54
        assert ev.duration_s == 301.2
        assert ev.failure_reason == "TIMEOUT: Agent exceeded 300s limit"

    def test_partial_conclusion_from_partial_output(self):
        result = _StubResult(
            tool_trace=({"tool": "Read", "target": "a.py"},),
            partial_output="  The adapter routes through _resolve.  ",
            output="TIMEOUT",
        )
        ev = build_partial_evidence(result)
        assert ev.partial_conclusion == "The adapter routes through _resolve."

    def test_is_empty_when_no_observable_work(self):
        result = _StubResult(output="TIMEOUT", failure_code="timeout")
        ev = build_partial_evidence(result)
        assert ev.is_empty() is True

    def test_not_empty_with_partial_conclusion_only(self):
        result = _StubResult(partial_output="I concluded X", output="TIMEOUT")
        ev = build_partial_evidence(result)
        assert ev.is_empty() is False

    def test_to_dict_roundtrips_fields(self):
        result = _StubResult(
            tool_trace=({"tool": "Read", "target": "a.py"},),
            partial_output="conclusion",
            output="TIMEOUT",
            failure_code="timeout",
            exit_code=-9,
            cost_usd=1.0,
        )
        d = build_partial_evidence(result, duration_s=10.0).to_dict()
        assert d["files_inspected"] == ["a.py"]
        assert d["tool_calls"] == [{"tool": "Read", "target": "a.py"}]
        assert d["partial_conclusion"] == "conclusion"
        assert d["failure_code"] == "timeout"
        assert d["duration_s"] == 10.0

    def test_truncates_overlong_conclusion(self):
        result = _StubResult(partial_output="x" * 9000, output="TIMEOUT")
        ev = build_partial_evidence(result)
        assert ev.partial_conclusion is not None
        assert len(ev.partial_conclusion) <= 4000
        assert ev.partial_conclusion.endswith("…")

    def test_ignores_malformed_trace_entries(self):
        result = _StubResult(
            tool_trace=("not a dict", {"tool": "Read", "target": "a.py"}, 42),
            output="TIMEOUT",
        )
        ev = build_partial_evidence(result)
        assert ev.tool_calls == ({"tool": "Read", "target": "a.py"},)


# ── render_partial_evidence ──────────────────────────────────────────────────


class TestRenderPartialEvidence:
    def test_none_and_empty_return_none(self):
        assert render_partial_evidence(None) is None
        assert render_partial_evidence({}) is None
        assert render_partial_evidence({"files_inspected": [], "tool_calls": []}) is None

    def test_renders_files_calls_and_conclusion(self):
        evidence = PreflightPartialEvidence(
            files_inspected=("src/a.py", "src/b.py"),
            tool_calls=(
                {"tool": "Read", "target": "src/a.py"},
                {"tool": "Grep", "target": "foo"},
            ),
            partial_conclusion="It routes through X.",
            failure_code="timeout",
        ).to_dict()
        rendered = render_partial_evidence(evidence)
        assert rendered is not None
        assert "src/a.py" in rendered
        assert "src/b.py" in rendered
        assert "Read src/a.py" in rendered
        assert "It routes through X." in rendered
        assert "timeout" in rendered

    def test_render_for_plan_empty_is_blank(self):
        assert PreflightPartialEvidence().render_for_plan() == ""
