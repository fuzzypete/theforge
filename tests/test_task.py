"""Tests for task.py prompt builders."""

from pathlib import Path

import pytest

from theforge.coord_state import CycleHistory
from theforge.review import ReviewFinding, ReviewResult, review_to_dev_handoff
from theforge.task import (
    TaskSpec,
    build_dev_prompt,
    build_fix_prompt,
    build_handoff_fix_prompt,
    build_plan_prompt,
    build_plan_review_prompt,
    build_review_prompt,
)


def _make_task(tmp_path: Path) -> TaskSpec:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskSpec(
        name="Test Task",
        spec_path=spec,
        slug="test-task",
    )


class TestBuildDevPrompt:
    """Tests for build_dev_prompt()."""

    def test_no_scope_blocked_sentinel(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            spec_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
        )
        assert "SCOPE_BLOCKED" not in prompt

    def test_preamble_appears_before_spec_content(self, tmp_path):
        task = _make_task(tmp_path)
        spec_text = "## Acceptance criteria\n\n- Do the thing."
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            spec_content=spec_text,
            gate_command="make gate",
        )
        assert "acceptance criteria" in prompt.lower()
        assert "checklist" in prompt.lower()
        assert "dev_notes" in prompt
        # preamble must appear before the spec content itself
        preamble_idx = prompt.lower().index("acceptance criteria are the definitive checklist")
        spec_idx = prompt.index(spec_text)
        assert preamble_idx < spec_idx


class TestBuildFixPrompt:
    """Tests for the minimal fix prompt used on iteration 2+."""

    def test_contains_workspace_info(self, tmp_path):
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        prompt = build_fix_prompt(
            task,
            workspace_path=workspace,
            branch_name="feat/test-task",
            review_findings="- P1: Off by one in src/foo.py:10",
            gate_command="make gate",
        )
        assert str(workspace) in prompt
        assert "feat/test-task" in prompt

    def test_contains_review_findings(self, tmp_path):
        task = _make_task(tmp_path)
        findings = "- P1: Missing null check in src/foo.py:42\n- P2: Typo in comment"
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings=findings,
            gate_command="make gate",
        )
        assert findings in prompt

    def test_does_not_contain_spec_content(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
        )
        # Spec content should NOT appear
        assert "Do the thing." not in prompt

    def test_does_not_contain_implementation_steps(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
        )
        # Full 9-step implementation instructions should NOT appear
        assert "Read the spec above carefully before writing any code" not in prompt
        assert "Implementation Steps" not in prompt

    def test_does_not_contain_preflight_section(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
        )
        assert "Codebase Context (from preflight)" not in prompt
        assert "preflight" not in prompt.lower()

    def test_instructs_agent_not_to_run_gate(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
        )
        # Should tell agent NOT to run the gate
        assert "Do NOT re-run the gate" in prompt
        assert "make gate" in prompt  # gate name mentioned but flagged as off-limits

    def test_includes_iteration_number(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            iteration=3,
        )
        assert "iteration 3" in prompt

    def test_default_iteration_is_2(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
        )
        assert "iteration 2" in prompt

    def test_instructs_make_fmt(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
        )
        assert "make fmt" in prompt

    def test_instructs_commit(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
        )
        assert "git commit" in prompt

    def test_gate_skipped_changes_instructions(self, tmp_path):
        task = _make_task(tmp_path)
        normal = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            gate_skipped=False,
        )
        skipped = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            gate_skipped=True,
        )
        # Normal: tells agent not to run gate (coordinator handles it)
        assert "Do NOT re-run the gate" in normal
        # Skip: gate note omitted entirely
        assert "Do NOT re-run the gate" not in skipped
        assert "Gate:" not in skipped
        assert "make gate" not in skipped


class TestBuildFixPromptCycleHistory:
    """Tests for cycle history and escalation note in build_fix_prompt."""

    def _make_history(self) -> list[CycleHistory]:
        return [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="Null check missing",
                p1_findings=["Missing null check in src/foo.py"],
            ),
            CycleHistory(
                cycle=2,
                verdict="REQUEST_CHANGES",
                summary="Still not fixed",
                p1_findings=["Missing null check in src/foo.py", "Type error in bar.py"],
            ),
        ]

    def test_includes_cycle_history_section(self, tmp_path):
        task = _make_task(tmp_path)
        history = self._make_history()
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            cycle_history=history,
        )
        assert "Previous Review Cycles" in prompt
        assert "Cycle 1: REQUEST_CHANGES" in prompt
        assert "Cycle 2: REQUEST_CHANGES" in prompt
        assert "Missing null check in src/foo.py" in prompt

    def test_no_history_section_when_empty(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            cycle_history=[],
        )
        assert "Previous Review Cycles" not in prompt

    def test_no_history_section_when_none(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            cycle_history=None,
        )
        assert "Previous Review Cycles" not in prompt

    def test_includes_escalation_note(self, tmp_path):
        task = _make_task(tmp_path)
        note = "MODEL ESCALATION: A P1 finding persisted. Old: sonnet. New: opus."
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            escalation_note=note,
        )
        assert "Model Escalation" in prompt
        assert "MODEL ESCALATION" in prompt

    def test_no_escalation_section_when_none(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            escalation_note=None,
        )
        assert "Model Escalation" not in prompt

    def test_p1_findings_shown_per_cycle(self, tmp_path):
        task = _make_task(tmp_path)
        history = [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="Summary one",
                p1_findings=["Alpha finding", "Beta finding"],
            ),
        ]
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            cycle_history=history,
        )
        assert "Alpha finding" in prompt
        assert "Beta finding" in prompt


class TestBuildDevPromptEscalation:
    """build_dev_prompt renders escalation note and cycle history when provided.

    build_dev_prompt handles first-run, gate-fail, reject, and timeout-resume paths.
    """

    def test_no_history_section_when_no_cycle_history(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            review_findings="P1: bug",
        )
        assert "Previous Review Cycles" not in prompt

    def test_cycle_history_rendered_when_provided(self, tmp_path):
        """Cycle history is injected into build_dev_prompt on reject path (post-cycle 1+)."""
        from theforge.coord_state import CycleHistory

        task = _make_task(tmp_path)
        history = [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="Missing tests.",
                p1_findings=["No tests for foo"],
            ),
        ]
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            cycle_history=history,
        )
        assert "Previous Review Cycles" in prompt
        assert "Cycle 1: REQUEST_CHANGES" in prompt
        assert "Missing tests." in prompt
        assert "No tests for foo" in prompt

    def test_no_escalation_when_not_provided(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
        )
        assert "Model Escalation" not in prompt

    def test_escalation_note_rendered_when_provided(self, tmp_path):
        """Escalation note is shown on reject-after-escalation path."""
        task = _make_task(tmp_path)
        note = "MODEL ESCALATION: Persistent P1. Old: sonnet. New: opus."
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            escalation_note=note,
        )
        assert "Model Escalation" in prompt
        assert "MODEL ESCALATION" in prompt

    def test_escalation_note_rendered_without_review_findings(self, tmp_path):
        """Escalation note appears even when review_findings is None (reject path)."""
        task = _make_task(tmp_path)
        note = "MODEL ESCALATION: Upgraded."
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            review_findings=None,
            escalation_note=note,
        )
        assert "Model Escalation" in prompt


class TestBuildPlanPrompt:
    """Tests for build_plan_prompt()."""

    def test_contains_spec_content(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec\n\nDo the unique thing.",
        )
        assert "Do the unique thing." in prompt

    def test_instructs_no_code(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
        )
        assert "Do NOT write code" in prompt

    def test_output_starts_with_implementation_plan(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
        )
        assert "# Implementation Plan" in prompt

    def test_includes_preflight_output_when_provided(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
            preflight_output="verdict: PROCEED\ncomplexity: medium",
        )
        assert "verdict: PROCEED" in prompt

    def test_omits_preflight_section_when_absent(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
            preflight_output=None,
        )
        assert "Preflight Analysis" not in prompt


class TestBuildDevPromptPlanOutput:
    """Tests for build_dev_prompt() with plan_output parameter."""

    def test_with_plan_output_includes_plan_section(self, tmp_path):
        task = _make_task(tmp_path)
        plan_text = "# Implementation Plan\n\nStep 1: do this."
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
            plan_output=plan_text,
        )
        assert "Implementation Plan (from planning agent)" in prompt
        assert plan_text in prompt

    def test_without_plan_output_omits_plan_section(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
        )
        assert "Implementation Plan (from planning agent)" not in prompt

    def test_plan_section_appears_before_spec(self, tmp_path):
        task = _make_task(tmp_path)
        plan_text = "# Implementation Plan\n\nUnique plan content here."
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec\n\nUnique spec content here.",
            gate_command="make gate",
            plan_output=plan_text,
        )
        plan_pos = prompt.index("Implementation Plan (from planning agent)")
        spec_pos = prompt.index("## Spec")
        assert plan_pos < spec_pos


# ── build_review_prompt role specialization ───────────────────────────


_REVIEW_COMMON_KWARGS = dict(
    spec_content="# Spec",
    commit_log="abc1234 feat(foo): implement the thing\ndef5678 test(foo): add tests",
    workspace_path="/tmp/ws",
    branch="feat/test",
    handoff_content="gate_decision: PASS",
)


@pytest.fixture
def review_task(tmp_path: Path) -> TaskSpec:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskSpec(
        name="Test Task",
        spec_path=spec,
        slug="test-task",
    )


class TestBuildReviewPrompt:
    """Tests for build_review_prompt() role specialization."""

    def test_default_no_role(self, review_task: TaskSpec) -> None:
        """review_role=None produces the generic prompt."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "You are a code reviewer." in prompt
        assert "safe to merge" in prompt

    def test_correctness_role(self, review_task: TaskSpec) -> None:
        """review_role='correctness' produces correctness-focused lens."""
        prompt = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="correctness"
        )
        assert "correctness" in prompt
        assert "Data integrity risks" in prompt
        assert "logic bugs" in prompt
        assert "API boundaries" not in prompt

    def test_patterns_role(self, review_task: TaskSpec) -> None:
        """review_role='patterns' produces patterns-focused lens."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role="patterns")
        assert "patterns" in prompt
        assert "API boundaries" in prompt
        assert "Error handling completeness" in prompt
        assert "Data integrity risks" not in prompt

    def test_edge_cases_role(self, review_task: TaskSpec) -> None:
        """review_role='edge-cases' produces edge-case-focused lens."""
        prompt = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="edge-cases"
        )
        assert "edge cases" in prompt
        assert "Boundary conditions" in prompt
        assert "Failure under unexpected input" in prompt
        assert "API boundaries" not in prompt

    def test_unknown_role_falls_back(self, review_task: TaskSpec) -> None:
        """Unknown review_role falls back to the generic prompt."""
        prompt_unknown = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="unknown-role"
        )
        prompt_none = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role=None)
        assert prompt_unknown == prompt_none

    def test_empty_string_role_falls_back(self, review_task: TaskSpec) -> None:
        """Empty string review_role falls back to the generic prompt."""
        prompt_empty = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role="")
        prompt_none = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert prompt_empty == prompt_none

    def test_shared_structure_across_roles(self, review_task: TaskSpec) -> None:
        """All roles share the same YAML output format and severity rules."""
        for role in [None, "correctness", "patterns", "edge-cases", "unknown"]:
            prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role=role)
            assert "verdict: APPROVE | REQUEST_CHANGES" in prompt
            assert "severity: P1 | P2" in prompt
            assert "## Severity Definitions" in prompt
            assert "## Rules" in prompt

    def test_includes_task_name(self, review_task: TaskSpec) -> None:
        """Task name appears in the prompt header."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "Test Task" in prompt

    def test_includes_commit_log(self, review_task: TaskSpec) -> None:
        """Commit log is embedded in the prompt as primary handoff."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "## Commits" in prompt
        assert "abc1234 feat(foo): implement the thing" in prompt
        assert "def5678 test(foo): add tests" in prompt
        assert "git show" in prompt
        assert "Changed Files (git diff --stat)" not in prompt

    def test_includes_tool_instructions(self, review_task: TaskSpec) -> None:
        """Reviewer is instructed to use Read/Bash/Glob/Grep tools."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "Read" in prompt
        assert "Bash" in prompt
        assert "Glob" in prompt
        assert "Grep" in prompt
        assert "/tmp/ws" in prompt
        assert "feat/test" in prompt

    def test_includes_spec(self, review_task: TaskSpec) -> None:
        """Spec content is embedded in the prompt."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "# Spec" in prompt
        # ## Spec heading must be on its own line (newline before it)
        assert "\n        ## Spec" in prompt or "\n## Spec" in prompt


# ── build_review_prompt cycle_history ────────────────────────────────


class TestBuildReviewPromptCycleHistory:
    """Tests for build_review_prompt() cycle_history parameter."""

    def _make_history(self) -> list[CycleHistory]:
        return [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="Null check missing in foo",
                p1_findings=["Missing null check in src/foo.py:42"],
            ),
        ]

    def test_cycle1_no_framing_when_history_none(self, review_task: TaskSpec) -> None:
        """Cycle 1 (no history): prompt unchanged — no tri-part framing."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=None,
        )
        assert "Cycle-Aware Review Framing" not in prompt
        assert "Verify Fixes" not in prompt
        assert "Scan Regressions" not in prompt

    def test_cycle1_no_framing_when_history_empty(self, review_task: TaskSpec) -> None:
        """Cycle 1 with empty list: same as None."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=[],
        )
        assert "Cycle-Aware Review Framing" not in prompt

    def test_cycle2_includes_tri_part_framing(self, review_task: TaskSpec) -> None:
        """Cycle 2+: prompt includes all three framing parts."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=self._make_history(),
        )
        assert "Cycle-Aware Review Framing" in prompt
        assert "Part 1" in prompt
        assert "Part 2" in prompt
        assert "Part 3" in prompt

    def test_cycle2_lists_prior_p1_findings(self, review_task: TaskSpec) -> None:
        """Cycle 2+: prior P1 findings are listed under Verify Fixes."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=self._make_history(),
        )
        assert "Missing null check in src/foo.py:42" in prompt
        assert "Cycle 1" in prompt

    def test_cycle2_shows_correct_cycle_number(self, review_task: TaskSpec) -> None:
        """Cycle 2+: cycle number in header reflects current cycle."""
        history = self._make_history()  # 1 cycle → reviewing cycle 2
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=history,
        )
        assert "review cycle 2" in prompt

    def test_cycle3_shows_all_prior_p1s(self, review_task: TaskSpec) -> None:
        """Cycle 3+: findings from all prior cycles are listed."""
        history = [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="First cycle issues",
                p1_findings=["Alpha finding from cycle 1"],
            ),
            CycleHistory(
                cycle=2,
                verdict="REQUEST_CHANGES",
                summary="Second cycle issues",
                p1_findings=["Beta finding from cycle 2"],
            ),
        ]
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=history,
        )
        assert "Alpha finding from cycle 1" in prompt
        assert "Beta finding from cycle 2" in prompt
        assert "review cycle 3" in prompt

    def test_cycle1_still_has_standard_sections(self, review_task: TaskSpec) -> None:
        """Cycle 1 prompt still has Severity Definitions and Rules."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=None,
        )
        assert "## Severity Definitions" in prompt
        assert "## Rules" in prompt

    def test_cycle2_also_has_standard_sections(self, review_task: TaskSpec) -> None:
        """Cycle 2 prompt keeps standard sections in addition to framing."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=self._make_history(),
        )
        assert "## Severity Definitions" in prompt
        assert "## Rules" in prompt
        assert "## Commits" in prompt


# ── TaskSpec.depends_on ──────────────────────────────────────────────


class TestTaskSpecDependsOn:
    def test_depends_on_default_empty(self, tmp_path: Path) -> None:
        """TaskSpec without depends_on argument defaults to []."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(name="Test", spec_path=spec, slug="test")
        assert task.depends_on == []

    def test_depends_on_list(self, tmp_path: Path) -> None:
        """TaskSpec accepts depends_on as a list of strings."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(name="Test", spec_path=spec, slug="test", depends_on=["a", "b"])
        assert task.depends_on == ["a", "b"]


# ── review_to_dev_handoff ─────────────────────────────────────────────


def _make_review_result(
    verdict: str = "REQUEST_CHANGES",
    summary: str = "Review summary.",
    findings: list[ReviewFinding] | None = None,
    spec_matches: bool = True,
    spec_mismatches: list[str] | None = None,
    test_adequate: bool = True,
    test_gaps: list[str] | None = None,
) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary=summary,
        findings=findings or [],
        spec_matches=spec_matches,
        spec_mismatches=spec_mismatches or [],
        test_adequate=test_adequate,
        test_gaps=test_gaps or [],
        parse_errors=[],
        raw_yaml={},
    )


class TestReviewToDevHandoff:
    def test_full_result(self):
        finding = ReviewFinding(
            severity="P1",
            file="src/foo.py",
            line=42,
            description="Off by one",
            suggestion="Fix the index",
        )
        result = _make_review_result(
            summary="Found a bug.",
            findings=[finding],
            spec_matches=False,
            spec_mismatches=["Missing batch config"],
            test_adequate=False,
            test_gaps=["No edge case test"],
        )
        output = review_to_dev_handoff(result)
        assert "## Review Summary" in output
        assert "Found a bug." in output
        assert "## Spec Compliance Issues" in output
        assert "Missing batch config" in output
        assert "## Missing Test Coverage" in output
        assert "No edge case test" in output
        assert "## Findings" in output
        assert "### [P1]" in output
        assert "**Issue:** Off by one" in output
        assert "**Fix:** Fix the index" in output

    def test_empty_findings(self):
        result = _make_review_result(
            summary="All good.",
            findings=[],
            spec_matches=True,
            test_adequate=True,
        )
        output = review_to_dev_handoff(result)
        assert "## Review Summary" in output
        assert "## Findings" in output
        assert "No findings." in output
        assert "## Spec Compliance Issues" not in output
        assert "## Missing Test Coverage" not in output

    def test_no_mismatches(self):
        finding = ReviewFinding(
            severity="P2",
            file="src/bar.py",
            line=10,
            description="Minor issue",
            suggestion="Improve it",
        )
        result = _make_review_result(
            summary="Minor issue only.",
            findings=[finding],
            spec_matches=True,
            test_adequate=True,
        )
        output = review_to_dev_handoff(result)
        assert "## Spec Compliance Issues" not in output
        assert "## Missing Test Coverage" not in output
        assert "### [P2]" in output

    def test_finding_no_line(self):
        finding = ReviewFinding(
            severity="P1",
            file="src/foo.py",
            line=None,
            description="Missing validation",
            suggestion="Add it",
        )
        result = _make_review_result(findings=[finding])
        output = review_to_dev_handoff(result)
        assert "### [P1] `src/foo.py`" in output
        # Header line should not contain "(line ...)" when line is None
        header_line = [ln for ln in output.splitlines() if "### [P1]" in ln][0]
        assert "(line" not in header_line

    def test_finding_no_suggestion(self):
        finding = ReviewFinding(
            severity="P2",
            file="src/foo.py",
            line=5,
            description="Needs cleanup",
            suggestion=None,
        )
        result = _make_review_result(findings=[finding])
        output = review_to_dev_handoff(result)
        assert "**Issue:** Needs cleanup" in output
        assert "**Fix:**" not in output

    def test_spec_false_but_empty_mismatches_omits_section(self):
        result = _make_review_result(spec_matches=False, spec_mismatches=[])
        output = review_to_dev_handoff(result)
        assert "## Spec Compliance Issues" not in output

    def test_test_adequate_false_but_empty_gaps_omits_section(self):
        result = _make_review_result(test_adequate=False, test_gaps=[])
        output = review_to_dev_handoff(result)
        assert "## Missing Test Coverage" not in output


# ── build_review_prompt dev_notes ─────────────────────────────────────


class TestBuildReviewPromptDevNotes:
    def test_dev_notes_present(self, review_task: TaskSpec) -> None:
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            dev_notes="I deviated from spec because X.",
        )
        assert "## Developer Notes" in prompt
        assert "I deviated from spec because X." in prompt
        # Dev Notes section appears before Commits
        assert prompt.index("## Developer Notes") < prompt.index("## Commits")

    def test_dev_notes_none(self, review_task: TaskSpec) -> None:
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, dev_notes=None)
        assert "## Developer Notes" not in prompt

    def test_dev_notes_empty_string(self, review_task: TaskSpec) -> None:
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, dev_notes="")
        assert "## Developer Notes" not in prompt


# ── build_dev_prompt dev_notes instruction ────────────────────────────


class TestBuildDevPromptDevNotesInstruction:
    def test_gate_not_skipped_includes_dev_notes(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(name="Test", spec_path=spec, slug="test")
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            gate_skipped=False,
        )
        assert "dev_notes" in prompt
        assert "handoff.yaml" in prompt

    def test_gate_skipped_excludes_gate_command(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(name="Test", spec_path=spec, slug="test")
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            gate_skipped=True,
        )
        assert "Gate is disabled" in prompt
        assert "make gate" not in prompt


# ── build_plan_review_prompt ──────────────────────────────────────────


class TestBuildPlanReviewPrompt:
    def test_contains_story_and_plan(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story\n\nDo the unique thing.",
            plan_content="# Plan\n\nStep 1: implement X.",
        )
        assert "Do the unique thing." in prompt
        assert "Step 1: implement X." in prompt

    def test_contains_evaluation_criteria(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
        )
        assert "APPROVE" in prompt
        assert "REJECT" in prompt
        assert "Acceptance criteria coverage" in prompt

    def test_includes_rejection_findings(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
            rejection_findings="- [P1] Bad API reference",
        )
        assert "Previous Rejection Findings" in prompt
        assert "Bad API reference" in prompt

    def test_omits_rejection_findings_when_none(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
            rejection_findings=None,
        )
        assert "Previous Rejection Findings" not in prompt


# ── build_handoff_fix_prompt ──────────────────────────────────────────


class TestBuildHandoffFixPrompt:
    def test_contains_validation_errors(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        errors = ["summary must be a non-empty string", "spec_deviations is required"]
        prompt = build_handoff_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            validation_errors=errors,
        )
        assert "summary must be a non-empty string" in prompt
        assert "spec_deviations is required" in prompt

    def test_contains_workspace_info(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        ws = tmp_path / "ws"
        prompt = build_handoff_fix_prompt(
            task,
            workspace_path=ws,
            branch_name="feat/test",
            validation_errors=["error"],
        )
        assert str(ws) in prompt
        assert "feat/test" in prompt

    def test_contains_required_format(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_handoff_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            validation_errors=["error"],
        )
        assert "spec_deviations" in prompt
        assert "deferred_items" in prompt
        assert "summary" in prompt
        assert "commits" in prompt
        assert "acceptance_criteria" in prompt
        assert "gate_result" in prompt

    def test_instructs_no_code_changes(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_handoff_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            validation_errors=["error"],
        )
        assert "Do NOT change any code" in prompt
        assert "Do NOT re-run the gate" in prompt


# ── build_dev_prompt structured dev_notes ─────────────────────────────


class TestBuildDevPromptStructuredHandoff:
    def test_gate_not_skipped_includes_structured_format(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(name="Test", spec_path=spec, slug="test")
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            gate_skipped=False,
        )
        # Must instruct all structured YAML fields
        assert "spec_deviations" in prompt
        assert "deferred_items" in prompt
        assert "summary" in prompt
        assert "commits" in prompt
        assert "acceptance_criteria" in prompt
        assert "gate_result" in prompt
        # Explains why structured handoff matters
        assert "your voice in the review" in prompt.lower()
