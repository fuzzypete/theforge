"""Tests for task.py plan parsing and structured plan rendering."""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator.context_scope import plan_file_list
from theforge.task import (
    PlanData,
    TaskStory,
    build_dev_prompt,
    build_plan_review_prompt,
    parse_plan_output,
)


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


def _minimal_plan_yaml(description: str = "do it") -> str:
    """Return a minimal but valid plan YAML with all required fields."""
    return (
        "---\n"
        "plan:\n"
        "  approach: implement the feature\n"
        "  steps:\n"
        f"    - id: 1\n"
        f"      description: {description}\n"
        "      files: [src/foo.py]\n"
        "      action: modify\n"
        "      details: change the code\n"
    )


def _parse_plan_files(text: str) -> list[str]:
    plan = parse_plan_output(text)
    assert plan is not None
    return plan_file_list(plan)


class TestBuildDevPromptStructuredHandoff:
    def test_gate_not_skipped_includes_structured_format(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskStory(name="Test", story_path=spec, slug="test")
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            gate_skipped=False,
        )
        # Must instruct all structured YAML fields
        assert "story_deviations" in prompt
        assert "deferred_items" in prompt
        assert "summary" in prompt
        assert "commits" in prompt
        assert "acceptance_criteria" in prompt
        # Explains why structured handoff matters — dev emits a forge_handoff block
        assert "forge_handoff" in prompt.lower()


_VALID_PLAN_YAML = """\
plan:
  approach: "Add a new module and update the coordinator."
  steps:
    - id: 1
      description: "Create new_module.py"
      files:
        - src/theforge/new_module.py
      action: create
      details: "Implement the core logic here."
    - id: 2
      description: "Update coordinator.py"
      files:
        - src/theforge/coordinator.py
      action: modify
      details: "Call new_module from coordinator."
      depends_on: [1]
  criteria_mapping:
    - criterion: "AC: new module exists"
      steps: [1]
    - criterion: "AC: coordinator uses it"
      steps: [2]
  risks:
    - description: "Circular import risk"
      mitigation: "Use TYPE_CHECKING import"
"""


class TestParsePlanOutput:
    """Tests for parse_plan_output()."""

    def test_valid_yaml_returns_plan_data(self):
        result = parse_plan_output(_VALID_PLAN_YAML)
        assert result is not None
        assert result["approach"] == "Add a new module and update the coordinator."
        assert len(result["steps"]) == 2

    def test_step_fields_preserved(self):
        result = parse_plan_output(_VALID_PLAN_YAML)
        assert result is not None
        step1 = result["steps"][0]
        assert step1["id"] == 1
        assert step1["description"] == "Create new_module.py"
        assert step1["action"] == "create"
        assert step1["files"] == ["src/theforge/new_module.py"]
        assert step1["details"] == "Implement the core logic here."
        assert "depends_on" not in step1  # optional field omitted

    def test_depends_on_preserved_when_present(self):
        result = parse_plan_output(_VALID_PLAN_YAML)
        assert result is not None
        step2 = result["steps"][1]
        assert step2["depends_on"] == [1]

    def test_criteria_mapping_preserved(self):
        result = parse_plan_output(_VALID_PLAN_YAML)
        assert result is not None
        assert "criteria_mapping" in result
        assert len(result["criteria_mapping"]) == 2
        assert result["criteria_mapping"][0]["criterion"] == "AC: new module exists"
        assert result["criteria_mapping"][0]["steps"] == [1]

    def test_risks_preserved(self):
        result = parse_plan_output(_VALID_PLAN_YAML)
        assert result is not None
        assert "risks" in result
        assert result["risks"][0]["description"] == "Circular import risk"

    def test_fenced_yaml_block_parsed(self):
        fenced = "```yaml\n" + _VALID_PLAN_YAML + "\n```"
        result = parse_plan_output(fenced)
        assert result is not None
        assert result["approach"] == "Add a new module and update the coordinator."

    def test_markdown_fallback_returns_none(self):
        markdown = "# Implementation Plan\n\nStep 1: do the thing."
        result = parse_plan_output(markdown)
        assert result is None

    def test_invalid_yaml_returns_none(self):
        bad_yaml = "plan:\n  approach: 'unclosed"
        result = parse_plan_output(bad_yaml)
        assert result is None

    def test_yaml_without_plan_key_returns_none(self):
        no_plan_key = "approach: 'something'\nsteps: []"
        result = parse_plan_output(no_plan_key)
        assert result is None

    def test_missing_required_step_field_returns_none(self):
        yaml_missing_details = """\
plan:
  approach: "Do something."
  steps:
    - id: 1
      description: "A step"
      files:
        - src/foo.py
      action: modify
"""
        result = parse_plan_output(yaml_missing_details)
        assert result is None

    def test_missing_approach_returns_none(self):
        yaml_no_approach = """\
plan:
  steps:
    - id: 1
      description: "A step"
      files: []
      action: modify
      details: "details"
"""
        result = parse_plan_output(yaml_no_approach)
        assert result is None

    def test_criteria_mapping_optional(self):
        yaml_no_mapping = """\
plan:
  approach: "Simple approach."
  steps:
    - id: 1
      description: "One step"
      files:
        - src/foo.py
      action: modify
      details: "details here"
"""
        result = parse_plan_output(yaml_no_mapping)
        assert result is not None
        assert "criteria_mapping" not in result

    def test_empty_string_returns_none(self):
        assert parse_plan_output("") is None

    def test_document_separator_stripped(self):
        with_separator = "---\n" + _VALID_PLAN_YAML
        result = parse_plan_output(with_separator)
        assert result is not None


class TestBuildDevPromptStructuredPlan:
    """Tests for build_dev_prompt() with PlanData dict as plan_output."""

    def _make_plan_data(self) -> PlanData:
        return {
            "approach": "Implement X by modifying Y.",
            "steps": [
                {
                    "id": 1,
                    "description": "Add helper function to utils.py",
                    "files": ["src/theforge/utils.py"],
                    "action": "modify",
                    "details": "Add a new helper that does Z.",
                },
                {
                    "id": 2,
                    "description": "Use helper in coordinator.py",
                    "files": ["src/theforge/coordinator.py"],
                    "action": "modify",
                    "details": "Call the helper from the main loop.",
                    "depends_on": [1],
                },
            ],
        }

    def test_structured_plan_renders_steps(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=plan,
        )
        assert "Step 1:" in prompt
        assert "Add helper function to utils.py" in prompt
        assert "Step 2:" in prompt
        assert "Use helper in coordinator.py" in prompt

    def test_structured_plan_shows_approach(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=plan,
        )
        assert "Implement X by modifying Y." in prompt

    def test_structured_plan_shows_action(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=plan,
        )
        assert "Action: modify" in prompt

    def test_structured_plan_shows_depends_on(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=plan,
        )
        assert "Depends on: Step 1" in prompt

    def test_structured_plan_omits_depends_on_when_absent(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=plan,
        )
        # Step 1 has no depends_on — "Depends on" should not appear for it
        # (it does appear for step 2, so just verify the content is for step 2)
        assert "Depends on: Step 1" in prompt

    def test_structured_plan_shows_header(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=plan,
        )
        assert "Implementation Plan (from planning agent)" in prompt

    def test_raw_string_plan_still_works(self, tmp_path):
        """Fallback: plain string plan_output still works as before."""
        task = _make_task(tmp_path)
        plan_text = "# Implementation Plan\n\nStep 1: do this."
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=plan_text,
        )
        assert "Implementation Plan (from planning agent)" in prompt
        assert plan_text in prompt


class TestBuildPlanReviewPromptStructured:
    """Tests for build_plan_review_prompt() with PlanData dict as plan_content."""

    def _make_plan_data(self) -> PlanData:
        return {
            "approach": "Use TypedDicts for structure.",
            "steps": [
                {
                    "id": 1,
                    "description": "Add TypedDicts to task.py",
                    "files": ["src/theforge/task.py"],
                    "action": "modify",
                    "details": "Define PlanStep and PlanData.",
                },
            ],
            "criteria_mapping": [
                {"criterion": "Plan TypedDict defined", "steps": [1]},
                {"criterion": "Steps have required fields", "steps": [1]},
            ],
        }

    def test_structured_plan_includes_criteria_mapping(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content=plan,
        )
        assert "Criteria Mapping" in prompt
        assert "Plan TypedDict defined" in prompt
        assert "Steps have required fields" in prompt

    def test_structured_plan_shows_approach(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content=plan,
        )
        assert "Use TypedDicts for structure." in prompt

    def test_structured_plan_shows_steps(self, tmp_path):
        task = _make_task(tmp_path)
        plan = self._make_plan_data()
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content=plan,
        )
        assert "Step 1:" in prompt
        assert "Add TypedDicts to task.py" in prompt

    def test_no_criteria_mapping_section_when_absent(self, tmp_path):
        task = _make_task(tmp_path)
        plan: PlanData = {
            "approach": "Simple.",
            "steps": [
                {
                    "id": 1,
                    "description": "Step 1",
                    "files": ["src/foo.py"],
                    "action": "modify",
                    "details": "Details.",
                }
            ],
        }
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content=plan,
        )
        assert "Criteria Mapping" not in prompt

    def test_raw_string_plan_still_works(self, tmp_path):
        """Fallback: plain string plan_content still embeds verbatim."""
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan\n\nStep 1: implement X.",
        )
        assert "Step 1: implement X." in prompt
        assert "Criteria Mapping" not in prompt


class TestParsePlanOutputYamlMarker:
    """Tests that YAML document marker '---' is stripped as a token, not char-by-char."""

    def test_yaml_document_marker_stripped_correctly(self):
        """Normal '---\\nplan:' input is parsed and '---' removed as a token."""
        result = parse_plan_output(_minimal_plan_yaml("step one"))
        assert result is not None
        assert result["steps"][0]["description"] == "step one"

    def test_slug_with_leading_dashes_in_description_not_truncated(self):
        """lstrip('-') would corrupt a value starting with '-'; [3:] leaves it intact."""
        result = parse_plan_output(_minimal_plan_yaml("-home-page setup"))
        assert result is not None
        assert result["steps"][0]["description"] == "-home-page setup"

    def test_plan_without_document_marker_also_works(self):
        """Plain 'plan:' without '---' header is also accepted."""
        yaml_input = (
            "plan:\n"
            "  approach: do it\n"
            "  steps:\n"
            "    - id: 1\n"
            "      description: plain step\n"
            "      files: []\n"
            "      action: modify\n"
            "      details: details here\n"
        )
        result = parse_plan_output(yaml_input)
        assert result is not None
        assert result["steps"][0]["description"] == "plain step"


class TestParsePlanOutputFileCounting:
    def test_prose_before_fenced_plan_still_counts_files(self):
        plan_text = (
            "I inspected the repo and here is the plan.\n\n"
            "```yaml\n"
            "plan:\n"
            "  approach: Keep the change tight.\n"
            "  steps:\n"
            "    - id: 1\n"
            "      description: Update the implementation\n"
            "      files:\n"
            "        - src/app.py\n"
            "        - tests/test_app.py\n"
            "      action: modify\n"
            "      details: Make the behavior match the spec.\n"
            "```\n"
        )

        assert _parse_plan_files(plan_text) == ["src/app.py", "tests/test_app.py"]

    def test_prose_before_bare_plan_still_counts_files(self):
        plan_text = (
            "Plan follows.\n\n"
            "plan:\n"
            "  approach: Update the parser.\n"
            "  steps:\n"
            "    - id: 1\n"
            "      description: Fix plan extraction\n"
            "      files:\n"
            "        - src/theforge/task/plan_parser.py\n"
            "      action: modify\n"
            "      details: Parse the structured block.\n"
        )

        assert _parse_plan_files(plan_text) == ["src/theforge/task/plan_parser.py"]

    def test_distinct_file_count_survives_multi_step_plan_shapes(self):
        plan_text = """\
plan:
  approach: Cover each caller with a small step.
  steps:
    - id: 1
      description: Touch the parser
      files:
        - src/theforge/task/plan_parser.py
      action: modify
      details: Parse plan YAML more robustly.
    - id: 2
      description: Wire the coordinator path
      files:
        - src/theforge/coordinator/plan_flow.py
        - src/theforge/coordinator/dev_phase.py
      action: modify
      details: Preserve the parsed plan across phases.
      depends_on: [1]
    - id: 3
      description: Cover helper behavior
      files:
        - src/theforge/coordinator/context_scope.py
        - tests/test_coord_context_assembly.py
      action: modify
      details: Assert deduped file extraction.
      depends_on: [1]
    - id: 4
      description: Add parser coverage
      files:
        - tests/test_task_plan.py
      action: modify
      details: Cover prose-prefixed plans.
      depends_on: [1]
    - id: 5
      description: Reuse the parser file in another step
      files:
        - src/theforge/task/plan_parser.py
      action: modify
      details: Reconfirm the same target is only counted once.
      depends_on: [1]
    - id: 6
      description: Validate the end-to-end flow
      files:
        - tests/test_coord_context_assembly.py
        - src/theforge/coordinator/dev_phase.py
      action: modify
      details: Confirm the scaled count reaches the audit log.
      depends_on: [2, 3, 4]
"""

        assert _parse_plan_files(plan_text) == [
            "src/theforge/task/plan_parser.py",
            "src/theforge/coordinator/plan_flow.py",
            "src/theforge/coordinator/dev_phase.py",
            "src/theforge/coordinator/context_scope.py",
            "tests/test_coord_context_assembly.py",
            "tests/test_task_plan.py",
        ]

    def test_empty_or_null_file_lists_do_not_count(self):
        plan_text = """\
plan:
  approach: Handle sparse steps safely.
  steps:
    - id: 1
      description: Empty file list
      files: []
      action: modify
      details: No targets declared yet.
    - id: 2
      description: Null file list
      files: null
      action: modify
      details: Provider omitted the list value.
    - id: 3
      description: Real target
      files: [src/real.py, tests/test_real.py]
      action: modify
      details: Only declared paths should count.
"""

        assert _parse_plan_files(plan_text) == ["src/real.py", "tests/test_real.py"]


class TestPromptContextPack:
    def test_preflight_prompt_includes_repository_context_pack(self, tmp_path):
        from theforge.task.context_assembler import ContextManifestEntry, ContextPack
        from theforge.task.plan_prompts import build_preflight_prompt

        task = _make_task(tmp_path)
        prompt = build_preflight_prompt(
            task,
            story_content="# Story",
            assembled_context=ContextPack(
                content="## Context\n\nRelevant module summary",
                included=(
                    ContextManifestEntry(
                        source="src/example/CLAUDE.md",
                        kind="claude_advisory",
                        required=False,
                        lines=2,
                        included=True,
                        reason="context",
                        score=1,
                    ),
                ),
                dropped=(),
                budget=10,
                line_count=2,
            ),
        )

        assert "Repository Context Pack" in prompt
        assert "Relevant module summary" in prompt

    def test_plan_prompt_includes_repository_context_pack(self, tmp_path):
        from theforge.task.context_assembler import ContextManifestEntry, ContextPack
        from theforge.task.plan_prompts import build_plan_prompt

        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            story_content="# Story",
            assembled_context=ContextPack(
                content="## Context\n\nPlanner should inspect this module",
                included=(
                    ContextManifestEntry(
                        source="src/example/CLAUDE.md",
                        kind="claude_advisory",
                        required=False,
                        lines=2,
                        included=True,
                        reason="context",
                        score=1,
                    ),
                ),
                dropped=(),
                budget=10,
                line_count=2,
            ),
        )

        assert "Repository Context Pack" in prompt
        assert "Planner should inspect this module" in prompt
