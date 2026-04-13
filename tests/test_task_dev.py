"""Tests for task.py dev/fix/plan prompt builders."""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator.state import CycleHistory
from theforge.task import (
    TaskStory,
    build_dev_prompt,
    build_fix_prompt,
    build_plan_prompt,
    build_preflight_prompt,
)


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
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
            story_content="# Spec\n\nDo the thing.",
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
            story_content=spec_text,
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

    def test_carry_forward_section_precedes_review_feedback(self, tmp_path):
        from theforge.coordinator.state import FindingRecord

        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="- P1: Current reviewer phrasing",
            gate_command="make gate",
            prior_open_p1s=[
                FindingRecord(
                    finding_id="abc123",
                    cycle_first_seen=1,
                    cycle_last_seen=2,
                    file="src/foo.py",
                    line=42,
                    severity="P1",
                    description="Original verbatim finding text",
                    reporter="reviewer-a",
                    disposition="unresolved",
                )
            ],
        )

        carry_idx = prompt.index("## Still-open Findings from Prior Review")
        review_idx = prompt.index("## Review Findings")
        assert carry_idx < review_idx
        assert "file=src/foo.py, line=42: Original verbatim finding text" in prompt

    def test_no_carry_forward_section_when_no_prior_open_p1s(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            review_findings="P1: bug",
            gate_command="make gate",
            prior_open_p1s=None,
        )
        assert "Still-open Findings from Prior Review" not in prompt

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
            story_content="# Spec",
            gate_command="make gate",
            review_findings="P1: bug",
        )
        assert "Previous Review Cycles" not in prompt

    def test_cycle_history_rendered_when_provided(self, tmp_path):
        """Cycle history is injected into build_dev_prompt on reject path (post-cycle 1+)."""
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
            story_content="# Spec",
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
            story_content="# Spec",
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
            story_content="# Spec",
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
            story_content="# Spec",
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
            story_content="# Spec\n\nDo the unique thing.",
        )
        assert "Do the unique thing." in prompt

    def test_instructs_no_code(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            story_content="# Spec",
        )
        assert "Do NOT write code" in prompt

    def test_output_requests_yaml_format(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            story_content="# Spec",
        )
        assert "plan:" in prompt
        assert "YAML" in prompt or "yaml" in prompt

    def test_includes_preflight_output_when_provided(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            story_content="# Spec",
            preflight_output="verdict: PROCEED\ncomplexity: medium",
        )
        assert "verdict: PROCEED" in prompt

    def test_omits_preflight_section_when_absent(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            story_content="# Spec",
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
            story_content="# Spec\n\nDo the thing.",
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
            story_content="# Spec\n\nDo the thing.",
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
            story_content="# Spec\n\nUnique spec content here.",
            gate_command="make gate",
            plan_output=plan_text,
        )
        plan_pos = prompt.index("Implementation Plan (from planning agent)")
        spec_pos = prompt.index("## Spec")
        assert plan_pos < spec_pos


# ── Notes section convention tests ──────────────────────────────────────


class TestNotesConventionInPrompts:
    """Verify that all spec-consuming prompts include Notes guidance."""

    def test_dev_prompt_contains_notes_guidance(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec\n\nDo the thing.\n\n## Notes\n\nSee src/old.py",
            gate_command="make gate",
        )
        assert "Notes" in prompt
        assert "stale or wrong" in prompt

    def test_plan_prompt_contains_notes_guidance(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_plan_prompt(
            task,
            story_content="# Spec\n\nDo the thing.\n\n## Notes\n\nSee src/old.py",
        )
        assert "Notes" in prompt
        assert "stale or wrong" in prompt

    def test_preflight_prompt_contains_notes_guidance(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_preflight_prompt(
            task,
            story_content="# Spec\n\nDo the thing.\n\n## Notes\n\nSee src/old.py",
        )
        assert "Notes" in prompt
        assert "NOT requirements" in prompt


class TestTestCommandInPrompts:
    """Tests that test_command is injected into dev and fix prompts."""

    def test_dev_prompt_includes_test_command_when_different_from_gate(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            story_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
            test_command="pytest tests/ -v -n auto --dist worksteal",
        )
        assert "pytest tests/ -v -n auto --dist worksteal" in prompt

    def test_dev_prompt_omits_test_section_when_test_command_equals_gate(self, tmp_path):
        task = _make_task(tmp_path)
        cmd = "make gate"
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            story_content="# Spec\n\nDo the thing.",
            gate_command=cmd,
            test_command=cmd,
        )
        assert "Testing During Development" not in prompt

    def test_dev_prompt_omits_test_section_when_test_command_is_none(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            story_content="# Spec\n\nDo the thing.",
            gate_command="make gate",
        )
        assert "Testing During Development" not in prompt

    def test_fix_prompt_includes_test_command_when_different_from_gate(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            review_findings="- P1: Bug in src/foo.py:10",
            gate_command="make gate",
            test_command="pytest tests/ -v -n auto --dist worksteal",
        )
        assert "pytest tests/ -v -n auto --dist worksteal" in prompt

    def test_fix_prompt_omits_test_bullet_when_test_command_equals_gate(self, tmp_path):
        task = _make_task(tmp_path)
        cmd = "make gate"
        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test-task",
            review_findings="- P1: Bug in src/foo.py:10",
            gate_command=cmd,
            test_command=cmd,
        )
        assert "hand-roll pytest" not in prompt
