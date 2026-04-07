"""Tests for task.py review prompt builders and review handoff utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.coordinator.state import CycleHistory
from theforge.review import ReviewFinding, ReviewResult, review_to_dev_handoff
from theforge.task import (
    TaskStory,
    build_dev_prompt,
    build_handoff_fix_prompt,
    build_plan_review_prompt,
    build_review_prompt,
)


def _make_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


_REVIEW_COMMON_KWARGS = dict(
    story_content="# Spec",
    commit_log="abc1234 feat(foo): implement the thing\ndef5678 test(foo): add tests",
    commit_diffs="diff --git a/foo.py b/foo.py\n+print('hello')",
    workspace_path="/tmp/ws",
    branch="feat/test",
    handoff_content="gate_decision: PASS",
)


@pytest.fixture
def review_task(tmp_path: Path) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


def _make_review_result(
    verdict: str = "REQUEST_CHANGES",
    summary: str = "Review summary.",
    findings: list[ReviewFinding] | None = None,
    story_matches: bool = True,
    story_mismatches: list[str] | None = None,
    test_adequate: bool = True,
    test_gaps: list[str] | None = None,
) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary=summary,
        findings=findings or [],
        story_matches=story_matches,
        story_mismatches=story_mismatches or [],
        test_adequate=test_adequate,
        test_gaps=test_gaps or [],
        parse_errors=[],
        raw_yaml={},
    )


class TestBuildReviewPrompt:
    """Tests for build_review_prompt() role specialization."""

    def test_default_no_role(self, review_task: TaskStory) -> None:
        """review_role=None produces the generic prompt."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "You are a code reviewer." in prompt
        assert "safe to merge" in prompt

    def test_correctness_role(self, review_task: TaskStory) -> None:
        """review_role='correctness' produces correctness-focused lens."""
        prompt = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="correctness"
        )
        assert "correctness" in prompt
        assert "Data integrity risks" in prompt
        assert "logic bugs" in prompt
        assert "API boundaries" not in prompt

    def test_patterns_role(self, review_task: TaskStory) -> None:
        """review_role='patterns' produces patterns-focused lens."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role="patterns")
        assert "patterns" in prompt
        assert "API boundaries" in prompt
        assert "Error handling completeness" in prompt
        assert "Data integrity risks" not in prompt

    def test_edge_cases_role(self, review_task: TaskStory) -> None:
        """review_role='edge-cases' produces edge-case-focused lens."""
        prompt = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="edge-cases"
        )
        assert "edge cases" in prompt
        assert "Boundary conditions" in prompt
        assert "Failure under unexpected input" in prompt
        assert "API boundaries" not in prompt

    def test_unknown_role_falls_back(self, review_task: TaskStory) -> None:
        """Unknown review_role falls back to the generic prompt."""
        prompt_unknown = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, review_role="unknown-role"
        )
        prompt_none = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role=None)
        assert prompt_unknown == prompt_none

    def test_empty_string_role_falls_back(self, review_task: TaskStory) -> None:
        """Empty string review_role falls back to the generic prompt."""
        prompt_empty = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role="")
        prompt_none = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert prompt_empty == prompt_none

    def test_shared_structure_across_roles(self, review_task: TaskStory) -> None:
        """All roles share the same YAML output format and severity rules."""
        for role in [None, "correctness", "patterns", "edge-cases", "unknown"]:
            prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, review_role=role)
            assert "verdict: APPROVE | REQUEST_CHANGES" in prompt
            assert "severity: P1 | P2" in prompt
            assert "## Severity Definitions" in prompt
            assert "## Rules" in prompt

    def test_includes_task_name(self, review_task: TaskStory) -> None:
        """Task name appears in the prompt header."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "Test Task" in prompt

    def test_includes_commit_log(self, review_task: TaskStory) -> None:
        """Commit log and embedded diffs are included in the prompt."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "## Commits" in prompt
        assert "Commit history:" in prompt
        assert "abc1234 feat(foo): implement the thing" in prompt
        assert "def5678 test(foo): add tests" in prompt
        assert "Full diffs:" in prompt
        assert "diff --git a/foo.py b/foo.py" in prompt
        assert "Use `git show <sha>`" not in prompt
        assert "Changed Files (git diff --stat)" not in prompt

    def test_includes_tool_instructions(self, review_task: TaskStory) -> None:
        """Reviewer is instructed to use Read/Bash/Glob/Grep tools."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "Read" in prompt
        assert "Bash" in prompt
        assert "Glob" in prompt
        assert "/tmp/ws" in prompt
        assert "feat/test" in prompt

    def test_includes_spec(self, review_task: TaskStory) -> None:
        """Spec content is embedded in the prompt."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "# Spec" in prompt
        # ## Spec heading must be on its own line (newline before it)
        assert "\n        ## Spec" in prompt or "\n## Spec" in prompt


class TestBuildReviewPromptCycleHistory:
    """Tests for build_review_prompt() cycle_history parameter."""

    def _make_history(self) -> list[CycleHistory]:
        return [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="Null check missing in foo",
                p1_findings=["Missing null check in src/foo.py:42"],
            ),
        ]

    def test_cycle1_no_framing_when_history_none(self, review_task: TaskStory) -> None:
        """Cycle 1 (no history): prompt unchanged — no tri-part framing."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=None,
        )
        assert "Cycle-Aware Review Framing" not in prompt
        assert "Verify Fixes" not in prompt
        assert "Scan Regressions" not in prompt

    def test_cycle1_no_framing_when_history_empty(self, review_task: TaskStory) -> None:
        """Cycle 1 with empty list: same as None."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=[],
        )
        assert "Cycle-Aware Review Framing" not in prompt

    def test_cycle2_includes_tri_part_framing(self, review_task: TaskStory) -> None:
        """Cycle 2+: prompt includes all three framing parts."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=self._make_history(),
        )
        assert "Cycle-Aware Review Framing" in prompt
        assert "Part 1" in prompt
        assert "Part 2" in prompt
        assert "Part 3" in prompt

    def test_cycle2_lists_prior_p1_findings(self, review_task: TaskStory) -> None:
        """Cycle 2+: prior P1 findings are listed under Verify Fixes."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=self._make_history(),
        )
        assert "Missing null check in src/foo.py:42" in prompt
        assert "Cycle 1" in prompt

    def test_cycle2_shows_correct_cycle_number(self, review_task: TaskStory) -> None:
        """Cycle 2+: cycle number in header reflects current cycle."""
        history = self._make_history()  # 1 cycle → reviewing cycle 2
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=history,
        )
        assert "review cycle 2" in prompt

    def test_cycle3_shows_all_prior_p1s(self, review_task: TaskStory) -> None:
        """Cycle 3+: findings from all prior cycles are listed."""
        history = [
            CycleHistory(
                cycle=1,
                verdict="REQUEST_CHANGES",
                summary="First cycle issues",
                p1_findings=["Alpha finding from cycle 1"],
            ),
            CycleHistory(
                cycle=2,
                verdict="REQUEST_CHANGES",
                summary="Second cycle issues",
                p1_findings=["Beta finding from cycle 2"],
            ),
        ]
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=history,
        )
        assert "Alpha finding from cycle 1" in prompt
        assert "Beta finding from cycle 2" in prompt
        assert "review cycle 3" in prompt

    def test_cycle1_still_has_standard_sections(self, review_task: TaskStory) -> None:
        """Cycle 1 prompt still has Severity Definitions and Rules."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=None,
        )
        assert "## Severity Definitions" in prompt
        assert "## Rules" in prompt

    def test_cycle2_also_has_standard_sections(self, review_task: TaskStory) -> None:
        """Cycle 2 prompt keeps standard sections in addition to framing."""
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            cycle_history=self._make_history(),
        )
        assert "## Severity Definitions" in prompt
        assert "## Rules" in prompt
        assert "## Commits" in prompt


class TestTaskStoryDependsOn:
    def test_depends_on_default_empty(self, tmp_path: Path) -> None:
        """TaskStory without depends_on argument defaults to []."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskStory(name="Test", story_path=spec, slug="test")
        assert task.depends_on == []

    def test_depends_on_list(self, tmp_path: Path) -> None:
        """TaskStory accepts depends_on as a list of strings."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskStory(name="Test", story_path=spec, slug="test", depends_on=["a", "b"])
        assert task.depends_on == ["a", "b"]


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
            story_matches=False,
            story_mismatches=["Missing batch config"],
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
            story_matches=True,
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
            story_matches=True,
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
        result = _make_review_result(story_matches=False, story_mismatches=[])
        output = review_to_dev_handoff(result)
        assert "## Spec Compliance Issues" not in output

    def test_test_adequate_false_but_empty_gaps_omits_section(self):
        result = _make_review_result(test_adequate=False, test_gaps=[])
        output = review_to_dev_handoff(result)
        assert "## Missing Test Coverage" not in output


class TestBuildReviewPromptDevNotes:
    def test_dev_notes_present(self, review_task: TaskStory) -> None:
        prompt = build_review_prompt(
            review_task,
            **_REVIEW_COMMON_KWARGS,
            dev_notes="I deviated from spec because X.",
        )
        assert "## Developer Notes" in prompt
        assert "I deviated from spec because X." in prompt
        # Dev Notes section appears before Commits
        assert prompt.index("## Developer Notes") < prompt.index("## Commits")

    def test_dev_notes_none(self, review_task: TaskStory) -> None:
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, dev_notes=None)
        assert "## Developer Notes" not in prompt

    def test_dev_notes_empty_string(self, review_task: TaskStory) -> None:
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS, dev_notes="")
        assert "## Developer Notes" not in prompt


class TestBuildDevPromptDevNotesInstruction:
    def test_gate_not_skipped_includes_dev_notes(self, tmp_path: Path) -> None:
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
        assert "dev_notes" in prompt
        assert "handoff.yaml" in prompt

    def test_gate_skipped_excludes_gate_command(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        task = TaskStory(name="Test", story_path=spec, slug="test")
        prompt = build_dev_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            story_content="# Spec",
            gate_command="make gate",
            gate_skipped=True,
        )
        assert "Gate is disabled" in prompt
        assert "make gate" not in prompt


class TestBuildPlanReviewPrompt:
    def test_contains_story_and_plan(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story\n\nDo the unique thing.",
            plan_content="# Plan\n\nStep 1: implement X.",
        )
        assert "Do the unique thing." in prompt
        assert "Step 1: implement X." in prompt

    def test_contains_evaluation_criteria(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
        )
        assert "APPROVE" in prompt
        assert "REJECT" in prompt
        assert "Acceptance criteria coverage" in prompt

    def test_includes_rejection_findings(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_plan_review_prompt(
            task,
            story_content="# Story",
            plan_content="# Plan",
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
            rejection_findings=None,
        )
        assert "Previous Rejection Findings" not in prompt


class TestBuildHandoffFixPrompt:
    def test_contains_validation_errors(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        errors = ["summary must be a non-empty string", "story_deviations is required"]
        prompt = build_handoff_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            validation_errors=errors,
        )
        assert "summary must be a non-empty string" in prompt
        assert "story_deviations is required" in prompt

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
        assert "story_deviations" in prompt
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

    def test_uses_configured_handoff_file(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path)
        prompt = build_handoff_fix_prompt(
            task,
            workspace_path=tmp_path / "ws",
            branch_name="feat/test",
            validation_errors=["error"],
            handoff_file=".forge/handoff.yaml",
        )
        assert ".forge/handoff.yaml" in prompt
        assert "git add .forge/handoff.yaml" in prompt


# ── Notes section convention tests ──────────────────────────────────────


class TestReviewPromptEvidenceRules:
    """Verify review prompt includes evidence-quality rules added after Gemini audit."""

    def test_verify_before_asserting(self, review_task: TaskStory) -> None:
        """Prompt requires reviewers to verify assumptions before filing P1."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "Verify before asserting" in prompt
        assert "unverified assumption" in prompt

    def test_list_all_issues_single_pass(self, review_task: TaskStory) -> None:
        """Prompt instructs reviewers to list all issues in one pass."""
        prompt = build_review_prompt(review_task, **_REVIEW_COMMON_KWARGS)
        assert "List ALL issues in a single pass" in prompt

    def test_dev_notes_claims_not_evidence(self, review_task: TaskStory) -> None:
        """Dev notes section frames notes as claims requiring verification."""
        prompt = build_review_prompt(
            review_task, **_REVIEW_COMMON_KWARGS, dev_notes="Deviation is justified."
        )
        assert "Developer notes are claims, not" in prompt
        assert "verify technical claims" in prompt


class TestNotesConventionInReviewPrompt:
    """Verify review prompt includes Notes guidance."""

    def test_review_prompt_contains_notes_guidance(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_review_prompt(
            task,
            story_content="# Spec\n\n## Notes\n\nSee src/old.py",
            commit_log="abc1234 feat: thing",
            commit_diffs="diff --git a/foo.py b/foo.py",
            workspace_path="/tmp/ws",
            branch="feat/test",
            handoff_content="gate_decision: PASS",
        )
        assert "Notes" in prompt
        assert "stale or wrong" in prompt
        assert "NOT acceptance criteria" in prompt


def test_review_prompt_includes_repository_context_pack(review_task):
    from theforge.task.context_assembler import ContextManifestEntry, ContextPack

    prompt = build_review_prompt(
        review_task,
        **_REVIEW_COMMON_KWARGS,
        assembled_context=ContextPack(
            content="## Context\n\n- review the touched modules first",
            included=(
                ContextManifestEntry(
                    source="src/theforge/task/CLAUDE.md",
                    kind="claude_advisory",
                    required=False,
                    lines=2,
                    included=True,
                    reason="review context",
                    score=1,
                ),
            ),
            dropped=(),
            budget=10,
            line_count=2,
            phase="review",
        ),
    )

    assert "Repository Context Pack" in prompt
    assert "review the touched modules first" in prompt
