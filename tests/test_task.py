"""Tests for task.py prompt builders."""

from pathlib import Path

import pytest

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
        file_scope=["src/foo.py", "tests/test_foo.py"],
    )


class TestBuildDevPrompt:
    """Tests for build_dev_prompt() file_scope advisory language."""

    def test_non_empty_scope_uses_focus_language(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            spec_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
        )
        assert "Focus your changes" in prompt

    def test_non_empty_scope_does_not_contain_scope_blocked(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            spec_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
        )
        assert "SCOPE_BLOCKED" not in prompt

    def test_empty_scope_still_works(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
        task = TaskSpec(
            name="No Scope Task",
            spec_path=spec,
            slug="no-scope",
            file_scope=[],
        )
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/no-scope",
            spec_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
        )
        assert "no scope restriction" in prompt
        assert "SCOPE_BLOCKED" not in prompt


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


class TestBuildPlanPrompt:
    """Tests for build_plan_prompt()."""

    def test_contains_file_contents(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec\n\nDo the thing.",
            file_contents={"src/foo.py": "def foo(): pass"},
        )
        assert "src/foo.py" in prompt
        assert "def foo(): pass" in prompt

    def test_contains_spec_content(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec\n\nDo the unique thing.",
            file_contents={},
        )
        assert "Do the unique thing." in prompt

    def test_instructs_no_code(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
            file_contents={},
        )
        assert "Do NOT write any code" in prompt

    def test_output_starts_with_implementation_plan(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
            file_contents={},
        )
        assert "# Implementation Plan" in prompt

    def test_includes_preflight_output_when_provided(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
            file_contents={},
            preflight_output="verdict: PROCEED\ncomplexity: medium",
        )
        assert "verdict: PROCEED" in prompt

    def test_omits_preflight_section_when_absent(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
            file_contents={},
            preflight_output=None,
        )
        assert "Preflight Analysis" not in prompt

    def test_empty_file_scope_shows_placeholder(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            spec_content="# Spec",
            file_contents={},
        )
        assert "no file_scope defined" in prompt


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


_REVIEW_TASK_SCOPE = ["src/"]

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
        file_scope=_REVIEW_TASK_SCOPE,
    )


class TestBuildReviewPrompt:
    """Tests for build_review_prompt() role specialization."""

    def test_default_no_role(self, review_task: TaskSpec) -> None:
        """review_role=None produces the generic prompt."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "You are a code reviewer." in prompt
        assert "The implementation matches the spec" in prompt

    def test_correctness_role(self, review_task: TaskSpec) -> None:
        """review_role='correctness' produces correctness-focused lens."""
        prompt = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="correctness"
        )
        assert "correctness" in prompt
        assert "Data integrity risks" in prompt
        assert "Security issues" in prompt
        assert "API usage patterns" not in prompt

    def test_patterns_role(self, review_task: TaskSpec) -> None:
        """review_role='patterns' produces patterns-focused lens."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role="patterns")
        assert "patterns" in prompt
        assert "API usage patterns" in prompt
        assert "Error handling completeness" in prompt
        assert "Data integrity risks" not in prompt

    def test_edge_cases_role(self, review_task: TaskSpec) -> None:
        """review_role='edge-cases' produces edge-case-focused lens."""
        prompt = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="edge-cases"
        )
        assert "edge cases" in prompt
        assert "Race conditions" in prompt
        assert "Boundary conditions" in prompt
        assert "API usage patterns" not in prompt

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


# ── TaskSpec.depends_on ──────────────────────────────────────────────


class TestTaskSpecDependsOn:
    def test_depends_on_default_empty(self, tmp_path: Path) -> None:
        """TaskSpec without depends_on argument defaults to []."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(name="Test", spec_path=spec, slug="test", file_scope=[])
        assert task.depends_on == []

    def test_depends_on_list(self, tmp_path: Path) -> None:
        """TaskSpec accepts depends_on as a list of strings."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(
            name="Test", spec_path=spec, slug="test", file_scope=[], depends_on=["a", "b"]
        )
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
        task = TaskSpec(name="Test", spec_path=spec, slug="test", file_scope=[])
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

    def test_gate_skipped_excludes_dev_notes(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskSpec(name="Test", spec_path=spec, slug="test", file_scope=[])
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            spec_content="# Spec",
            gate_command="make gate",
            gate_skipped=True,
        )
        assert "dev_notes" not in prompt


# ── build_plan_review_prompt ──────────────────────────────────────────


class TestBuildPlanReviewPrompt:
    def test_contains_story_and_plan(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story\n\nDo the unique thing.",
            plan_content="# Plan\n\nStep 1: implement X.",
            file_contents={},
        )
        assert "Do the unique thing." in prompt
        assert "Step 1: implement X." in prompt

    def test_contains_evaluation_criteria(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
            file_contents={},
        )
        assert "APPROVE" in prompt
        assert "REJECT" in prompt
        assert "acceptance criteria" in prompt

    def test_includes_file_contents(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
            file_contents={"src/foo.py": "def foo(): pass"},
        )
        assert "src/foo.py" in prompt
        assert "def foo(): pass" in prompt

    def test_includes_rejection_findings(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
            file_contents={},
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
            file_contents={},
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
        task = TaskSpec(name="Test", spec_path=spec, slug="test", file_scope=[])
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
        # Validates structure
        assert (
            "validates this structure" in prompt.lower()
            or "coordinator validates" in prompt.lower()
        )
