"""Tests for task.py prompt builders."""

from pathlib import Path

import pytest

from theforge.task import (
    TaskSpec,
    build_dev_prompt,
    build_fix_prompt,
    build_plan_prompt,
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
    diff_stat=" src/foo.py | 10 +++---\n 1 file changed",
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

    def test_includes_diff_stat(self, review_task: TaskSpec) -> None:
        """Diff stat summary is embedded in the prompt."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "Changed Files (git diff --stat)" in prompt
        assert "src/foo.py | 10" in prompt
        assert "```diff" not in prompt
        assert "## Diff to Review" not in prompt

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
