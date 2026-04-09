from __future__ import annotations

from pathlib import Path

from theforge.task import TaskStory, build_dev_prompt
from theforge.task.fix_prompts import build_handoff_fix_prompt


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


def test_dev_prompt_treats_gate_as_hard_prerequisite(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="make gate",
        handoff_file=".forge/handoff.yaml",
    )

    assert "hard prerequisite for done" in prompt
    assert "Do NOT declare success, write a completed handoff, or treat the task as" in prompt
    assert "Only after the gate passes, commit your changes" in prompt


def test_dev_prompt_handoff_example_does_not_self_report_gate_result(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="make gate",
        handoff_file=".forge/handoff.yaml",
    )

    assert "gate_result" not in prompt


def test_handoff_fix_prompt_does_not_require_gate_result_field(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    prompt = build_handoff_fix_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        validation_errors=["summary is required"],
        handoff_file=".forge/handoff.yaml",
    )

    assert "gate_result" not in prompt
