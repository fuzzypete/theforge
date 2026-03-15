"""Tests for task.py prompt builders."""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.task import TaskSpec, build_dev_prompt


@pytest.fixture()
def minimal_task(tmp_path: Path) -> TaskSpec:
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("# Spec\nDo something.")
    return TaskSpec(
        name="Test Task",
        spec_path=spec_file,
        slug="test-task",
        file_scope=["src/foo.py", "tests/test_foo.py"],
    )


def _make_prompt(task: TaskSpec, **kwargs) -> str:
    defaults = dict(
        workspace_path=Path("/workspace/test-task"),
        branch_name="feat/test-task",
        spec_content="# Spec\nDo something.",
        gate_command="make gate",
    )
    defaults.update(kwargs)
    return build_dev_prompt(task, **defaults)


class TestBuildDevPromptScopeInstruction:
    def test_scope_blocked_sentinel_in_prompt(self, minimal_task: TaskSpec) -> None:
        prompt = _make_prompt(minimal_task)
        assert "SCOPE_BLOCKED:" in prompt

    def test_no_commit_instruction(self, minimal_task: TaskSpec) -> None:
        prompt = _make_prompt(minimal_task)
        assert "Do NOT commit any code changes" in prompt

    def test_no_workaround_instruction(self, minimal_task: TaskSpec) -> None:
        prompt = _make_prompt(minimal_task)
        assert "Do NOT implement a workaround" in prompt

    def test_old_permissive_text_absent(self, minimal_task: TaskSpec) -> None:
        prompt = _make_prompt(minimal_task)
        assert "Add a note in your commit message" not in prompt

    def test_escalation_consequence_explained(self, minimal_task: TaskSpec) -> None:
        prompt = _make_prompt(minimal_task)
        assert "escalation" in prompt.lower()

    def test_file_scope_listed_in_prompt(self, minimal_task: TaskSpec) -> None:
        prompt = _make_prompt(minimal_task)
        assert "src/foo.py" in prompt
        assert "tests/test_foo.py" in prompt

    def test_no_scope_restriction_when_empty(self, minimal_task: TaskSpec) -> None:
        task = TaskSpec(
            name=minimal_task.name,
            spec_path=minimal_task.spec_path,
            slug=minimal_task.slug,
            file_scope=[],
        )
        prompt = _make_prompt(task)
        assert "no scope restriction — all project files" in prompt
