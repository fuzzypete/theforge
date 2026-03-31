"""Tests for plan review severity calibration.

Covers:
- merge_plan_review_results verdict for each severity category
- Advisory forwarding to dev agent context
- Mixed-severity scenarios
- parse_plan_review_output round-trip with P1-impl
- REQUEST_CHANGES normalization
- Prompt and API schema alignment
"""

from textwrap import dedent

from theforge.review import (
    PlanReviewFinding,
    PlanReviewResult,
    merge_plan_review_results,
    parse_plan_review_output,
    plan_review_findings_to_text,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _finding(severity: str, description: str = "some issue") -> PlanReviewFinding:
    return PlanReviewFinding(severity=severity, description=description, suggestion=None)


def _result(*findings: PlanReviewFinding) -> PlanReviewResult:
    return PlanReviewResult(verdict="APPROVE", findings=list(findings), parse_errors=[])


# ── Merge logic — verdict by severity ────────────────────────────────────────


def test_merge_p1_impl_only_approves():
    """P1-impl findings alone must not block — verdict is APPROVE."""
    r = _result(_finding("P1-impl", "edge case handling"), _finding("P1-impl", "key collision"))
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"


def test_merge_p0_rejects():
    """A single P0 finding forces REJECT."""
    r = _result(_finding("P0", "impossible constraint"))
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "REJECT"


def test_merge_p1_rejects():
    """A single P1 (architectural) finding forces REJECT."""
    r = _result(_finding("P1", "wrong API used"))
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "REJECT"


def test_merge_mixed_p1_and_p1_impl_rejects():
    """P1 presence forces REJECT even when P1-impl findings are also present."""
    r = _result(
        _finding("P1", "structural flaw"),
        _finding("P1-impl", "edge case to handle"),
    )
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "REJECT"


def test_merge_p1_impl_and_p2_approves():
    """P1-impl and P2 together are non-blocking — verdict is APPROVE."""
    r = _result(
        _finding("P1-impl", "implementation detail"),
        _finding("P2", "style suggestion"),
    )
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"


def test_merge_p2_only_approves():
    """P2-only findings approve (existing behavior preserved)."""
    r = _result(_finding("P2", "improvement idea"))
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"


def test_merge_no_findings_approves():
    """No findings → APPROVE."""
    r = _result()
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"


# ── Advisory forwarding — text serialization ─────────────────────────────────


def test_findings_to_text_includes_p1_impl():
    """plan_review_findings_to_text must include P1-impl findings with correct label."""
    r = PlanReviewResult(
        verdict="APPROVE",
        findings=[
            _finding("P1-impl", "watch key collision on update"),
            _finding("P2", "consider extracting helper"),
        ],
        parse_errors=[],
    )
    text = plan_review_findings_to_text(r)
    assert "[P1-impl]" in text
    assert "watch key collision on update" in text
    assert "[P2]" in text


def test_findings_to_text_label_format():
    """Severity label format is exactly [SEVERITY] — no extra tokens."""
    finding = _finding("P1-impl", "some detail")
    r = PlanReviewResult(verdict="APPROVE", findings=[finding], parse_errors=[])
    text = plan_review_findings_to_text(r)
    assert text.startswith("- [P1-impl]")


# ── Advisory forwarding — end-to-end coordinator wiring ──────────────────────


def test_approved_plan_with_p1_impl_populates_state_advisory():
    """Advisory wiring: merge → plan_review_findings_to_text → dev_prompt.

    Verifies the full advisory forwarding chain:
    1. merge_plan_review_results with P1-impl → APPROVE
    2. plan_review_findings_to_text serializes the finding
    3. build_dev_prompt includes the advisory text in its output
    """
    from theforge.task.dev_prompts import build_dev_prompt

    # Step 1: merge produces APPROVE with P1-impl findings
    r = _result(_finding("P1-impl", "watch key collision handling"))
    merged = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"

    # Step 2: serialize findings to advisory text
    advisory_text = plan_review_findings_to_text(merged)
    assert "P1-impl" in advisory_text
    assert "watch key collision" in advisory_text

    # Step 3: dev prompt incorporates advisory text
    import os
    import sys

    tests_dir = os.path.dirname(__file__)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)

    import tempfile

    from coord_test_helpers import _make_task

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        tmp_path = Path(tmp)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        prompt = build_dev_prompt(
            task,
            workspace_path=workspace,
            branch_name="feat/test",
            story_content="## Spec\n- AC: something",
            gate_command=".venv/bin/python -m pytest tests/ -q",
            plan_output="## Plan\n- step 1",
            plan_review_advisory=advisory_text,
        )
    assert "watch key collision" in prompt
    assert "Plan Review Notes" in prompt or "advisory" in prompt.lower()


# ── Parse round-trip ──────────────────────────────────────────────────────────


def test_parse_p1_impl_approve_not_demoted():
    """parse_plan_review_output: APPROVE + P1-impl finding must not be demoted to REJECT."""
    yaml_block = dedent("""\
        ```yaml
        verdict: APPROVE
        criteria_coverage: []
        findings:
          - severity: P1-impl
            description: "edge case for empty list"
            suggestion: "add a guard"
        ```
    """)
    result = parse_plan_review_output(yaml_block)
    assert result.verdict == "APPROVE"
    assert result.parse_errors == []
    assert len(result.findings) == 1
    assert result.findings[0].severity == "P1-impl"


def test_parse_p1_impl_without_fences():
    """P1-impl severity parses correctly from raw YAML (no fences)."""
    yaml_block = dedent("""\
        verdict: APPROVE
        findings:
          - severity: P1-impl
            description: "monotonic counter details"
    """)
    result = parse_plan_review_output(yaml_block)
    assert result.verdict == "APPROVE"
    assert result.findings[0].severity == "P1-impl"


# ── REQUEST_CHANGES normalization ─────────────────────────────────────────────


def test_request_changes_normalized_to_reject():
    """REQUEST_CHANGES verdict from API mode must normalize to REJECT without parse error."""
    yaml_block = dedent("""\
        ```yaml
        verdict: REQUEST_CHANGES
        findings:
          - severity: P1
            description: "structural flaw in approach"
        ```
    """)
    result = parse_plan_review_output(yaml_block)
    assert result.verdict == "REJECT"
    assert result.parse_errors == []
    assert len(result.findings) == 1


def test_request_changes_with_p1_impl_findings():
    """REQUEST_CHANGES with only P1-impl findings: normalizes to REJECT."""
    yaml_block = dedent("""\
        ```yaml
        verdict: REQUEST_CHANGES
        findings:
          - severity: P1-impl
            description: "watch edge case"
        ```
    """)
    result = parse_plan_review_output(yaml_block)
    # Normalizes to REJECT (reviewer chose REQUEST_CHANGES)
    assert result.verdict == "REJECT"
    assert result.parse_errors == []


def test_approve_unaffected_by_normalization():
    """APPROVE verdict is unaffected by the REQUEST_CHANGES normalization step."""
    yaml_block = dedent("""\
        ```yaml
        verdict: APPROVE
        findings: []
        ```
    """)
    result = parse_plan_review_output(yaml_block)
    assert result.verdict == "APPROVE"
    assert result.parse_errors == []


# ── Prompt alignment ──────────────────────────────────────────────────────────


def test_prompt_contains_p1_impl_definition(tmp_path):
    """build_plan_review_prompt must include P1-impl severity definition."""
    from theforge.task.plan_prompts import build_plan_review_prompt
    from theforge.task.story import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    task = TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
        pytest_target="tests/",
    )
    prompt = build_plan_review_prompt(
        task,
        story_content="## Spec\n- AC: something",
        plan_content="## Plan\n- step 1",
    )
    assert "P1-impl" in prompt
    assert "implementation detail" in prompt.lower() or "implementation concern" in prompt.lower()


def test_prompt_yaml_severity_enum_includes_p1_impl(tmp_path):
    """YAML output format example must include P1-impl in the severity enum."""
    from theforge.task.plan_prompts import build_plan_review_prompt
    from theforge.task.story import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    task = TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
        pytest_target="tests/",
    )
    prompt = build_plan_review_prompt(
        task,
        story_content="## Spec\n- AC: something",
        plan_content="## Plan\n- step 1",
        mode="cli",
    )
    # The YAML example block must show P1-impl as a valid severity value
    assert "P0 | P1 | P1-impl | P2" in prompt


# ── API schema alignment ──────────────────────────────────────────────────────


def test_api_schema_includes_p1_impl():
    """_submit_plan_review_schema severity enum must include P1-impl."""
    from theforge.runners.schema_utils import _submit_plan_review_schema

    schema = _submit_plan_review_schema()
    items_schema = schema["parameters"]["properties"]["findings"]["items"]
    severity_enum = items_schema["properties"]["severity"]["enum"]
    assert "P1-impl" in severity_enum
    assert "P1" in severity_enum
    assert "P2" in severity_enum
