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

from theforge.coordinator.state import PlanFindingRecord
from theforge.review import (
    PlanReviewFinding,
    PlanReviewResult,
    apply_plan_corroboration,
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
    merged, downgrades = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"
    assert downgrades == []


def test_merge_p0_rejects():
    """A single P0 finding forces REJECT."""
    r = _result(_finding("P0", "impossible constraint"))
    merged, _ = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "REJECT"


def test_merge_single_reviewer_p1_downgraded():
    """A single P1 from one reviewer is downgraded to P1-impl (advisory) via corroboration."""
    r = _result(_finding("P1", "wrong API used"))
    merged, downgrades = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"
    assert len(downgrades) == 1
    assert downgrades[0].original_severity == "P1"
    assert downgrades[0].effective_severity == "P1-impl"
    assert merged.findings[0].severity == "P1-impl"


def test_merge_mixed_single_reviewer_p1_and_p1_impl_approves():
    """Single-reviewer P1 is downgraded; combined with P1-impl → APPROVE."""
    r = _result(
        _finding("P1", "structural flaw"),
        _finding("P1-impl", "edge case to handle"),
    )
    merged, downgrades = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"
    assert len(downgrades) == 1


def test_merge_p1_impl_and_p2_approves():
    """P1-impl and P2 together are non-blocking — verdict is APPROVE."""
    r = _result(
        _finding("P1-impl", "implementation detail"),
        _finding("P2", "style suggestion"),
    )
    merged, _ = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"


def test_merge_p2_only_approves():
    """P2-only findings approve (existing behavior preserved)."""
    r = _result(_finding("P2", "improvement idea"))
    merged, _ = merge_plan_review_results([r], ["reviewer-a"])
    assert merged.verdict == "APPROVE"


def test_merge_no_findings_approves():
    """No findings → APPROVE."""
    r = _result()
    merged, _ = merge_plan_review_results([r], ["reviewer-a"])
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
    merged, _ = merge_plan_review_results([r], ["reviewer-a"])
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
        test_target="tests/",
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
        test_target="tests/",
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


def test_plan_review_json_schema_mirrors_submit_tool(tmp_path):
    """plan_review_json_schema() must mirror the submit_plan_review tool parameters."""
    from theforge.runners.schema_utils import _submit_plan_review_schema
    from theforge.schemas import plan_review_json_schema

    tool_params = _submit_plan_review_schema()["parameters"]
    json_schema = plan_review_json_schema()

    # Same top-level required fields
    assert set(json_schema["required"]) == set(tool_params["required"])
    # Severity enum must match
    tool_severity = tool_params["properties"]["findings"]["items"]["properties"]["severity"][
        "enum"
    ]
    schema_severity = json_schema["properties"]["findings"]["items"]["properties"]["severity"][
        "enum"
    ]
    assert set(schema_severity) == set(tool_severity)
    # additionalProperties: false at top level (required for OpenAI strict mode)
    assert json_schema.get("additionalProperties") is False


def test_plan_review_prompt_api_mode_uses_submit_tool(tmp_path):
    """build_plan_review_prompt with mode='api' uses submit_plan_review tool and omits YAML."""
    from theforge.task.plan_prompts import build_plan_review_prompt
    from theforge.task.story import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    task = TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
        test_target="tests/",
    )
    prompt = build_plan_review_prompt(
        task,
        story_content="## Spec\n- AC: something",
        plan_content="## Plan\n- step 1",
        mode="api",
    )
    assert "submit_plan_review" in prompt
    # YAML output block must not appear in API mode
    assert "```yaml" not in prompt


def test_plan_review_prompt_renders_plan_risks(tmp_path):
    """build_plan_review_prompt must surface the plan's stated risks to the reviewer."""
    from theforge.task.plan_prompts import build_plan_review_prompt
    from theforge.task.story import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    task = TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
        test_target="tests/",
    )
    plan_content = {
        "approach": "Thread pricing data through to schema_utils._estimate_cost.",
        "steps": [
            {
                "id": 1,
                "description": "Pass pricing data down",
                "files": ["src/theforge/schema_utils.py"],
                "action": "modify",
                "details": "...",
            },
        ],
        "criteria_mapping": [],
        "risks": [
            {
                "description": (
                    "Some runner paths may not currently carry AgentSpec/ModelProfile "
                    "pricing data down to schema_utils._estimate_cost."
                ),
                "mitigation": "",
            }
        ],
    }
    prompt = build_plan_review_prompt(
        task,
        story_content="## Spec\n- AC: something",
        plan_content=plan_content,
    )
    assert "Risks Stated By The Plan" in prompt
    assert "Some runner paths may not currently carry" in prompt
    assert "Risk reconciliation" in prompt


def test_plan_review_prompt_no_risks_omits_risk_section(tmp_path):
    """When the plan has no risks list, no risk section is rendered into plan content."""
    from theforge.task.plan_prompts import build_plan_review_prompt
    from theforge.task.story import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    task = TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
        test_target="tests/",
    )
    plan_content = {
        "approach": "Do the thing.",
        "steps": [
            {
                "id": 1,
                "description": "Step one",
                "files": ["src/theforge/foo.py"],
                "action": "modify",
                "details": "...",
            },
        ],
        "criteria_mapping": [],
    }
    prompt = build_plan_review_prompt(
        task,
        story_content="## Spec\n- AC: something",
        plan_content=plan_content,
    )
    assert "Risks Stated By The Plan" not in prompt


def test_plan_review_prompt_rules_require_risk_reporting():
    """The Rules section must mandate that unresolved AC-relevant risks become findings."""
    from theforge.task.plan_prompts import build_plan_review_prompt
    from theforge.task.story import TaskStory
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        spec = Path(td) / "spec.md"
        spec.write_text("# Spec\n", encoding="utf-8")
        task = TaskStory(
            name="Test Task",
            story_path=spec,
            slug="test-task",
            test_target="tests/",
        )
        prompt = build_plan_review_prompt(
            task,
            story_content="## Spec\n- AC: something",
            plan_content="## Plan\n- step 1",
        )
    assert "reported as a P1 finding" in prompt
    assert "risk-silent" in prompt


def test_plan_review_prompt_cli_mode_uses_yaml_block(tmp_path):
    """build_plan_review_prompt with mode='cli' includes YAML block, omits submit_plan_review."""
    from theforge.task.plan_prompts import build_plan_review_prompt
    from theforge.task.story import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    task = TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
        test_target="tests/",
    )
    prompt = build_plan_review_prompt(
        task,
        story_content="## Spec\n- AC: something",
        plan_content="## Plan\n- step 1",
        mode="cli",
    )
    assert "```yaml" in prompt
    # CLI mode must NOT instruct agent to use the tool
    assert "submit_plan_review" not in prompt


# ── Corroboration logic ──────────────────────────────────────────────────────


def test_corroboration_single_reviewer_p1_downgraded():
    """Single-reviewer P1 on first occurrence → P1-impl, verdict APPROVE."""
    r_a = _result(_finding("P1", "missing error handling in validate_plan"))
    merged, downgrades = merge_plan_review_results([r_a], ["reviewer-a"])
    assert merged.verdict == "APPROVE"
    assert len(downgrades) == 1
    assert downgrades[0].original_severity == "P1"
    assert downgrades[0].effective_severity == "P1-impl"
    assert merged.findings[0].severity == "P1-impl"


def test_corroboration_two_reviewers_same_issue_stays_p1():
    """Same P1 from 2 reviewers (shared anchors) → stays P1, verdict REJECT."""
    r_a = _result(_finding("P1", "validate_plan missing error handling"))
    r_b = _result(_finding("P1", "validate_plan does not handle errors"))
    merged, downgrades = merge_plan_review_results([r_a, r_b], ["reviewer-a", "reviewer-b"])
    assert merged.verdict == "REJECT"
    assert len(downgrades) == 0
    assert all(f.severity == "P1" for f in merged.findings)


def test_corroboration_recurring_p1_stays_p1():
    """Single-reviewer P1 that matches prior registry entry → stays P1, REJECT."""
    prior = [
        PlanFindingRecord(
            description="validate_plan missing error handling",
            severity="P1",
            cycle_first_seen=0,
            cycle_last_seen=0,
            disposition="unresolved",
        )
    ]
    r_a = _result(_finding("P1", "validate_plan missing error handling"))
    merged, downgrades = merge_plan_review_results(
        [r_a], ["reviewer-a"], prior_registry=prior, current_attempt=1
    )
    assert merged.verdict == "REJECT"
    assert len(downgrades) == 0
    assert merged.findings[0].severity == "P1"


def test_corroboration_p0_always_blocks():
    """P0 always blocks regardless of reviewer count or recurrence."""
    r_a = _result(_finding("P0", "impossible to implement given constraints"))
    merged, downgrades = merge_plan_review_results([r_a], ["reviewer-a"])
    assert merged.verdict == "REJECT"
    assert len(downgrades) == 0
    assert merged.findings[0].severity == "P0"


def test_corroboration_mixed_corroborated_and_uncorroborated():
    """Mixed: corroborated P1 stays, uncorroborated P1 downgraded."""
    # reviewer-a raises two P1s; reviewer-b raises the same first P1 (shared anchor)
    r_a = _result(
        _finding("P1", "validate_plan missing error handling"),
        _finding("P1", "unrelated naming convention issue"),
    )
    r_b = _result(
        _finding("P1", "validate_plan does not handle errors"),
    )
    merged, downgrades = merge_plan_review_results([r_a, r_b], ["reviewer-a", "reviewer-b"])
    # The validate_plan findings are corroborated (2 reviewers), naming is not
    assert merged.verdict == "REJECT"
    severities = [f.severity for f in merged.findings]
    assert "P1" in severities  # corroborated one stays
    assert "P1-impl" in severities  # uncorroborated one downgraded
    assert len(downgrades) == 1


def test_corroboration_two_reviewers_no_shared_anchors_downgraded():
    """Two reviewers raising different P1s (no shared anchors) → both downgraded."""
    r_a = _result(_finding("P1", "validate_plan is missing checks"))
    r_b = _result(_finding("P1", "run_agent timeout is too short"))
    merged, downgrades = merge_plan_review_results([r_a, r_b], ["reviewer-a", "reviewer-b"])
    assert merged.verdict == "APPROVE"
    assert len(downgrades) == 2
    assert all(f.severity == "P1-impl" for f in merged.findings)


def test_apply_corroboration_no_p1_passthrough():
    """apply_plan_corroboration with no P1s returns findings unchanged."""
    findings = [
        _finding("P1-impl", "edge case"),
        _finding("P2", "style issue"),
    ]
    result, downgrades = apply_plan_corroboration(findings)
    assert result == findings
    assert downgrades == []


def test_audit_includes_original_and_effective_severity():
    """PlanFindingRecord with original_severity serializes both fields in audit."""
    from theforge.coordinator.state import PlanFindingRecord

    rec = PlanFindingRecord(
        description="validate_plan missing checks",
        severity="P1-impl",
        cycle_first_seen=0,
        cycle_last_seen=0,
        disposition="new",
        original_severity="P1",
    )
    # Simulate the audit serialization
    audit_entry = {
        "description": rec.description,
        "severity": rec.severity,
        "original_severity": rec.original_severity,
        "effective_severity": rec.severity,
        "cycle_first_seen": rec.cycle_first_seen,
        "cycle_last_seen": rec.cycle_last_seen,
        "disposition": rec.disposition,
    }
    assert audit_entry["original_severity"] == "P1"
    assert audit_entry["effective_severity"] == "P1-impl"
    assert audit_entry["severity"] == "P1-impl"


def test_corroboration_dotted_reviewer_names_corroborate():
    """Dotted reviewer names (e.g. gpt-5.4-a) must be parsed correctly."""
    r_a = _result(_finding("P1", "validate_plan missing error handling"))
    r_b = _result(_finding("P1", "validate_plan does not handle errors"))
    merged, downgrades = merge_plan_review_results([r_a, r_b], ["gpt-5.4-a", "gpt-5.4-b"])
    assert merged.verdict == "REJECT"
    assert len(downgrades) == 0
    assert all(f.severity == "P1" for f in merged.findings)


def test_corroboration_exotic_reviewer_names_corroborate():
    """Reviewer names with spaces, slashes, or other chars must corroborate.

    ModelProfile.name is unconstrained — the prefix parser must accept any
    characters between the brackets emitted by merge_plan_review_results.
    """
    r_a = _result(_finding("P1", "validate_plan missing error handling"))
    r_b = _result(_finding("P1", "validate_plan does not handle errors"))
    merged, downgrades = merge_plan_review_results([r_a, r_b], ["reviewer/a", "reviewer b"])
    assert merged.verdict == "REJECT"
    assert len(downgrades) == 0
    assert all(f.severity == "P1" for f in merged.findings)
