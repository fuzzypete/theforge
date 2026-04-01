"""Tests for plan-obedience framing in build_dev_prompt."""

from __future__ import annotations

from pathlib import Path

from theforge.task import TaskStory, build_dev_prompt


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


PLAN_DICT = {
    "approach": "Do it this way",
    "steps": [
        {"id": 1, "description": "First step", "action": "edit", "details": "Edit the file"},
    ],
}

PLAN_STR = "1. Do the first thing\n2. Do the second thing"


class TestPlanObedienceFraming:
    """Verify strict vs relaxed plan-obedience language in the dev prompt."""

    # ── Strict (default / feature stories) ──────────────────────────────

    def test_strict_framing_dict_plan_no_signal(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=PLAN_DICT,
        )
        assert "Follow it closely" in prompt
        assert "adapt freely" not in prompt

    def test_strict_framing_str_plan_no_signal(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=PLAN_STR,
        )
        assert "Follow it closely" in prompt
        assert "adapt freely" not in prompt

    def test_strict_framing_explicit_needs_planning(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=PLAN_DICT,
            preflight_sufficiency="needs_planning",
        )
        assert "Follow it closely" in prompt
        assert "adapt freely" not in prompt

    # ── Relaxed (implementation_ready / note-driven / refactor stories) ─

    def test_relaxed_framing_dict_plan_implementation_ready(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=PLAN_DICT,
            preflight_sufficiency="implementation_ready",
        )
        assert "adapt freely" in prompt
        assert "Follow it closely" not in prompt

    def test_relaxed_framing_str_plan_implementation_ready(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=PLAN_STR,
            preflight_sufficiency="implementation_ready",
        )
        assert "adapt freely" in prompt
        assert "Follow it closely" not in prompt

    # ── No plan — obedience text irrelevant ─────────────────────────────

    def test_no_plan_section_absent(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            preflight_sufficiency="implementation_ready",
        )
        assert "Implementation Plan" not in prompt

    # ── Default backward-compat: no preflight_sufficiency → strict ──────

    def test_default_no_signal_is_strict(self, tmp_path):
        """When preflight_sufficiency is not passed, strict framing must be used."""
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            plan_output=PLAN_DICT,
            # preflight_sufficiency intentionally omitted
        )
        assert "Follow it closely" in prompt
