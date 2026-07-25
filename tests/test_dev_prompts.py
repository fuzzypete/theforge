"""Tests for plan-obedience framing in build_dev_prompt."""

from __future__ import annotations

from pathlib import Path

from theforge.config.defaults import DEFAULT_DEV_PROFILE
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


class TestInvestigationReadyDevPromptRouting:
    """Seam test: TaskStory.investigation_ready must change downstream dev-cycle
    behavior. The dev prompt is the runtime consumer of the typed signal —
    investigation-ready bugs are routed to cause discovery rather than
    hypothesized-cause implementation.
    """

    def test_investigation_ready_inserts_cause_discovery_section(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
        task = TaskStory(
            name="Bug: symptom only",
            story_path=spec,
            slug="bug",
            type="bug",
            fix_ready=True,
            investigation_ready=True,
        )
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
        )
        assert "Investigation-Ready Bug" in prompt
        assert "cause discovery" in prompt.lower()
        assert "not yet" in prompt.lower()
        # Must explicitly tell the dev agent NOT to treat the confirmed-cause
        # field as an implementation target.
        assert "implementation target" in prompt
        # Must reference the symptom-resolution gate the reviewer applies.
        assert "fix-success criterion" in prompt

    def test_implementation_ready_bug_omits_cause_discovery_section(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
        task = TaskStory(
            name="Bug: known cause",
            story_path=spec,
            slug="bug",
            type="bug",
            fix_ready=True,
            investigation_ready=False,
        )
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
        )
        assert "Investigation-Ready Bug" not in prompt

    def test_default_task_omits_cause_discovery_section(self, tmp_path):
        # Backwards-compat: tasks with default investigation_ready=False (e.g.
        # features, tasks) must not see the bug-cycle scaffolding.
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
        )
        assert "Investigation-Ready Bug" not in prompt


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


def test_dev_prompt_uses_configured_commands_and_no_hardcoded_layout_rule(tmp_path):
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="npm run validate",
        test_command="npm test -- --runInBand",
    )

    assert "npm run validate" in prompt
    assert "npm test -- --runInBand" in prompt
    assert "make fmt" not in prompt
    assert "make lint" not in prompt
    assert "Do not create files outside `src/`, `tests/`, or `docs/`" not in prompt
    assert "## Workflow" in prompt
    assert "1. Implement the spec. Write tests for new functionality." in prompt
    assert "2. Run the gate command to validate your work:" in prompt
    assert "```bash" in prompt
    assert "npm run validate" in prompt
    assert "3. Only after the gate passes, commit your changes:" in prompt
    assert "4. Emit a `<forge_handoff>` block in your **final message**" in prompt


def test_dev_prompt_requires_honest_gate_result_field(tmp_path):
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="make gate",
    )

    # The handoff block advertises the structured gate_result field...
    assert "gate_result: PASS | FAIL | BLOCKED" in prompt
    # ...and forbids claiming completion without gate evidence.
    assert "Set `gate_result: PASS` only after the gate command actually passed" in prompt
    assert "set `gate_result: BLOCKED`" in prompt
    # collapse whitespace to tolerate dedent/wrapping of the multi-line rule
    _flat = " ".join(prompt.split())
    assert "do\n" not in _flat  # sanity: flattened
    assert "NOT mark any acceptance criterion `MET`" in _flat


def test_dev_prompt_includes_webfetch_framing_when_tool_allowed(tmp_path):
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
        story_content="# Spec",
        gate_command="make gate",
    )

    assert "WebFetch" in prompt
    assert "untrusted" in prompt
    assert "--help" in prompt


def test_dev_prompt_defaults_to_in_scope_p2_policy(tmp_path):
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="make gate",
    )

    assert "## Dev P2 Policy" in prompt
    assert "Active mode: `in_scope`." in prompt
    assert "Fix those P2s now instead of deferring them" in prompt


def test_dev_prompt_all_policy_requires_all_p2s(tmp_path):
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="make gate",
        review_findings="- severity: P2",
        p2_policy="all",
    )

    assert "Active mode: `all`." in prompt
    assert "ALL P1 findings and ALL P2 findings" in prompt


def test_dev_prompt_p1_only_policy_keeps_p2s_advisory(tmp_path):
    task = _make_task(tmp_path)
    prompt = build_dev_prompt(
        task,
        workspace_path=tmp_path / "ws",
        branch_name="feat/test",
        story_content="# Spec",
        gate_command="make gate",
        review_findings="- severity: P2",
        p2_policy="p1_only",
    )

    assert "Active mode: `p1_only`." in prompt
    assert "P2 findings are advisory" in prompt


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
