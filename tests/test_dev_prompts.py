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


def test_dev_prompt_includes_repository_context_pack(tmp_path):
    task = _make_task(tmp_path)
    from theforge.task.context_assembler import ContextManifestEntry, ContextPack

    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="make gate",
        assembled_context=ContextPack(
            content="## Invariants\n\n- keep it deterministic",
            included=(
                ContextManifestEntry(
                    source="src/example/CLAUDE.md",
                    kind="claude_invariants",
                    required=True,
                    lines=2,
                    included=True,
                    reason="invariants",
                    score=0,
                ),
            ),
            dropped=(),
            budget=10,
            line_count=2,
        ),
    )

    assert "Repository Context Pack" in prompt
    assert "keep it deterministic" in prompt


class TestContractChangeTestRule:
    """Verify that the test-editing rule is conditional on contract_change."""

    def _make_prompt(self, tmp_path: Path, *, contract_change: bool) -> str:
        task = _make_task(tmp_path)
        return build_dev_prompt(
            task,
            workspace_path=tmp_path,
            branch_name="feat/test",
            story_content="# Test\n\nDo something.",
            gate_command="make test",
            contract_change=contract_change,
        )

    def test_default_false_uses_strict_rule(self, tmp_path: Path) -> None:
        prompt = self._make_prompt(tmp_path, contract_change=False)
        assert "implementation is wrong" in prompt
        assert "fix your code, not the tests" in prompt
        assert "intentionally changes an existing behavioral contract" not in prompt

    def test_contract_change_true_uses_permissive_rule(self, tmp_path: Path) -> None:
        prompt = self._make_prompt(tmp_path, contract_change=True)
        assert "intentionally changes an existing behavioral contract" in prompt
        assert "You MAY update test files" in prompt
        assert "implementation is wrong" not in prompt
        assert "fix your code, not the tests" not in prompt

    def test_contract_change_still_restricts_unrelated_tests(self, tmp_path: Path) -> None:
        prompt = self._make_prompt(tmp_path, contract_change=True)
        assert "Do NOT modify tests unrelated to the contract change" in prompt


class TestProviderSdkIsolationRule:
    """Verify that the provider SDK isolation rule is always present in dev prompts."""

    def _make_prompt(self, tmp_path: Path, *, contract_change: bool) -> str:
        task = _make_task(tmp_path)
        return build_dev_prompt(
            task,
            workspace_path=tmp_path,
            branch_name="feat/test",
            story_content="# Test\n\nDo something.",
            gate_command="make test",
            contract_change=contract_change,
        )

    def test_rule_present_without_contract_change(self, tmp_path: Path) -> None:
        prompt = self._make_prompt(tmp_path, contract_change=False)
        assert "optional provider SDKs" in prompt
        assert "mock or stub that boundary" in prompt

    def test_rule_present_with_contract_change(self, tmp_path: Path) -> None:
        prompt = self._make_prompt(tmp_path, contract_change=True)
        assert "optional provider SDKs" in prompt
        assert "mock or stub that boundary" in prompt
