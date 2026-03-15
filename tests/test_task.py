"""Tests for task.py prompt builders."""

from pathlib import Path

from theforge.task import TaskSpec, build_dev_prompt, build_fix_prompt, build_plan_prompt


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
        # ## Spec heading must be on its own line (newline before it)
        assert "\n        ## Spec" in prompt or "\n## Spec" in prompt
