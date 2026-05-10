"""Surface-level tests for intake remediation drop reporting.

When intake remediation drops a story (DROPPED_AFTER_FIX or DROPPED_SHAPE),
every operator-facing surface — run log, audit YAML, sprint summary YAML, and
forge sprint-status DETAIL column — must carry the rule code(s) that fired,
the human-readable problem, and the structured intake metadata. These tests
exercise the helpers that produce that data and the live-status renderer that
projects it. The runner-level wiring (which sets entry detail / records the
current_story_entries_by_ref entry) is exercised through the IntakeOutcome
helper API the runner uses; the runner block itself is hard to call without
the full sprint pipeline, so we keep the seam test focused on the helpers and
the renderer that consume their outputs.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import yaml

from theforge.intake.findings import FixType, IntakeFinding, IntakeSeverity
from theforge.intake.remediation import IntakeOutcome, IntakeOutcomeKind
from theforge.sprint.audit import _write_sprint_audit, _write_sprint_summary
from theforge.sprint.manifest import ResolvedSprint, SprintResult
from theforge.sprint.runner import (
    _intake_agent_summary,
    _intake_audit_block,
    _intake_error_type,
    _intake_finding_codes,
    _intake_log_lines,
    _intake_outcome_summary,
    _intake_problem_lines,
)
from theforge.sprint.status_reader import _stage_and_detail_from_live_story


def _finding(code: str, problem: str, location: str = "acceptance_criteria") -> IntakeFinding:
    return IntakeFinding(
        code=code,
        severity=IntakeSeverity.BLOCK,
        location=location,
        problem=problem,
        fix_type=FixType.SEMANTIC,
    )


def _outcome(
    kind: IntakeOutcomeKind = IntakeOutcomeKind.DROPPED_AFTER_FIX,
    *,
    findings: tuple[IntakeFinding, ...] = (),
    detail: str = "rerun gate still failing; did not edit issue",
    audit: dict | None = None,
) -> IntakeOutcome:
    return IntakeOutcome(
        slug="task-1",
        kind=kind,
        findings=findings,
        detail=detail,
        audit=audit or {"remediation_source": "agent"},
    )


def test_intake_finding_codes_dedup_and_preserve_order():
    f1 = _finding("groom_how_shaped_ac", "rule A fired")
    f2 = _finding("groom_how_shaped_ac", "duplicate of A")
    f3 = _finding("bug_missing_diagnosis", "rule B fired")
    outcome = _outcome(findings=(f1, f2, f3))
    assert _intake_finding_codes(outcome) == ["groom_how_shaped_ac", "bug_missing_diagnosis"]


def test_intake_outcome_summary_includes_codes_and_detail():
    outcome = _outcome(findings=(_finding("groom_how_shaped_ac", "ACs read like a HOW"),))
    summary = _intake_outcome_summary(outcome)
    # Operator must see both the rule code (so they know which gate fired) AND
    # the detail string (so they know whether the agent tried/failed/declined).
    assert "groom_how_shaped_ac" in summary
    assert "rerun gate still failing" in summary


def test_intake_outcome_summary_falls_back_to_kind_when_no_findings_or_detail():
    outcome = _outcome(findings=(), detail="")
    # Even a malformed outcome must surface *something* — never blank.
    assert _intake_outcome_summary(outcome) == IntakeOutcomeKind.DROPPED_AFTER_FIX.value


def test_intake_problem_lines_render_one_per_finding():
    f1 = _finding("groom_how_shaped_ac", "ACs prescribe implementation steps")
    f2 = _finding("missing_example", "no example block found", location="examples")
    outcome = _outcome(findings=(f1, f2))
    lines = _intake_problem_lines(outcome)
    assert len(lines) == 2
    assert "groom_how_shaped_ac" in lines[0]
    assert "ACs prescribe implementation steps" in lines[0]
    assert "(examples)" in lines[1]
    assert "no example block found" in lines[1]


def test_intake_error_type_uses_first_rule_code():
    # The runner uses this for outcome_code so audit/summary YAML and forge
    # sprint-status all classify dropped stories by the rule code that fired,
    # not the generic outcome name.
    outcome = _outcome(findings=(_finding("groom_how_shaped_ac", "p"),))
    assert _intake_error_type(outcome) == "groom_how_shaped_ac"


def test_intake_error_type_falls_back_to_kind_value_when_no_findings():
    outcome = _outcome(findings=())
    assert _intake_error_type(outcome) == "dropped_after_fix"


def test_intake_agent_summary_includes_attempted_cost_model_transport():
    outcome = _outcome(
        findings=(_finding("groom_how_shaped_ac", "p"),),
        audit={
            "remediation_source": "agent",
            "agent": {
                "attempted": True,
                "cost_usd": 0.083,
                "model_used": "claude-sonnet-4-6",
                "transport_used": "cli",
            },
            "issue_updated": False,
            "comment_posted": False,
        },
    )
    summary = _intake_agent_summary(outcome)
    assert "source=agent" in summary
    assert "agent_attempted=yes" in summary
    assert "$0.0830" in summary
    assert "model=claude-sonnet-4-6" in summary
    assert "transport=cli" in summary


def test_intake_agent_summary_when_agent_did_not_run():
    """Real DROPPED_SHAPE outcomes always carry an ``agent`` block (built by
    intake.remediation._build_audit) with attempted=False; the summary must
    explicitly say so rather than going silent."""
    outcome = _outcome(
        kind=IntakeOutcomeKind.DROPPED_SHAPE,
        findings=(_finding("missing_acceptance_criteria", "p", location="acceptance_criteria"),),
        detail="auto-fix disabled",
        audit={
            "remediation_source": "none",
            "agent": {
                "attempted": False,
                "detail": "",
                "profile_name": None,
                "model_used": None,
                "cost_usd": None,
                "transport_used": None,
            },
        },
    )
    summary = _intake_agent_summary(outcome)
    assert "agent_attempted=no" in summary


def test_intake_log_lines_for_dropped_after_fix_include_codes_problem_and_agent_attempt():
    """The runner emits these lines verbatim — operators reading the run log
    must learn the rule code, the finding problem string, AND whether the
    auto-fix tried (cost/model/transport) without opening audit YAML."""
    f = _finding("groom_how_shaped_ac", "ACs prescribe implementation steps")
    outcome = _outcome(
        findings=(f,),
        detail="rerun gate still failing; did not edit issue",
        audit={
            "remediation_source": "agent",
            "agent": {
                "attempted": True,
                "cost_usd": 0.083,
                "model_used": "claude-sonnet-4-6",
                "transport_used": "cli",
            },
        },
    )
    lines = _intake_log_lines(outcome, outcome_name="DROPPED_AFTER_FIX", display_key="Issue #1473")
    blob = "\n".join(lines)
    assert "DROPPED_AFTER_FIX Issue #1473" in blob
    assert "groom_how_shaped_ac" in blob
    assert "ACs prescribe implementation steps" in blob
    # Reviewer-required fields: agent attempted/cost/model/transport.
    assert "agent_attempted=yes" in blob
    assert "$0.0830" in blob
    assert "model=claude-sonnet-4-6" in blob
    assert "transport=cli" in blob
    assert "source=agent" in blob


def test_intake_audit_block_carries_codes_findings_and_audit():
    f = _finding("groom_how_shaped_ac", "p")
    outcome = _outcome(
        findings=(f,),
        audit={"remediation_source": "agent", "agent": {"attempted": True, "cost_usd": 0.083}},
    )
    block = _intake_audit_block(outcome)
    assert block["kind"] == "dropped_after_fix"
    assert block["codes"] == ["groom_how_shaped_ac"]
    assert block["findings"][0]["code"] == "groom_how_shaped_ac"
    assert block["findings"][0]["problem"] == "p"
    assert block["audit"]["agent"]["cost_usd"] == 0.083
    # detail must round-trip so summary YAML and audit YAML can both read it.
    assert block["detail"] == "rerun gate still failing; did not edit issue"


def _live_intake_detail_dict() -> dict:
    """Mirror the SprintStoryState.detail dict written by the runner."""
    return {
        "final_outcome": "DROPPED_AFTER_FIX",
        "intake_summary": "[groom_how_shaped_ac] rerun gate still failing; did not edit issue",
        "intake_codes": ["groom_how_shaped_ac"],
        "intake_kind": "dropped_after_fix",
        "intake_findings": [
            {
                "code": "groom_how_shaped_ac",
                "severity": "block",
                "location": "acceptance_criteria",
                "problem": "ACs prescribe implementation steps",
                "suggested_replacement": None,
                "fix_type": "semantic",
            }
        ],
        "intake_agent_summary": (
            "source=agent, agent_attempted=yes, cost=$0.0830, model=claude, transport=cli"
        ),
        "intake_audit": {
            "remediation_source": "agent",
            "agent": {
                "attempted": True,
                "cost_usd": 0.083,
                "model_used": "claude",
                "transport_used": "cli",
            },
        },
    }


def test_live_status_detail_for_dropped_after_fix_renders_problem_and_agent_summary():
    story = {
        "status": "failed",
        "outcome": "dropped_after_fix",
        "phase": None,
        "blocked_by": [],
        "detail": _live_intake_detail_dict(),
    }
    _, detail, _ = _stage_and_detail_from_live_story(story)
    assert "DROPPED_AFTER_FIX" in detail
    assert "groom_how_shaped_ac" in detail
    # Finding.problem must be visible — not just the rule code.
    assert "ACs prescribe implementation steps" in detail
    # Agent attempt detail must be visible — operator needs to know whether
    # the auto-fix tried, and at what cost/model.
    assert "agent_attempted=yes" in detail
    assert "$0.0830" in detail


def test_live_status_detail_synthesizes_agent_summary_from_intake_audit_when_field_missing():
    """If intake_agent_summary wasn't pre-rendered, derive it from intake_audit."""
    detail_dict = _live_intake_detail_dict()
    detail_dict.pop("intake_agent_summary", None)
    story = {
        "status": "failed",
        "outcome": "dropped_after_fix",
        "phase": None,
        "blocked_by": [],
        "detail": detail_dict,
    }
    _, detail, _ = _stage_and_detail_from_live_story(story)
    assert "agent_attempted=yes" in detail
    assert "$0.0830" in detail
    assert "model=claude" in detail


def test_live_status_detail_for_dropped_shape_includes_problem_string():
    story = {
        "status": "failed",
        "outcome": "dropped_shape",
        "phase": None,
        "blocked_by": [],
        "detail": {
            "final_outcome": "DROPPED_SHAPE",
            "intake_summary": "[missing_acceptance_criteria] auto-fix disabled",
            "intake_codes": ["missing_acceptance_criteria"],
            "intake_kind": "dropped_shape",
            "intake_findings": [
                {
                    "code": "missing_acceptance_criteria",
                    "severity": "block",
                    "location": "acceptance_criteria",
                    "problem": "no acceptance_criteria section found",
                    "suggested_replacement": None,
                    "fix_type": "mechanical",
                }
            ],
            "intake_audit": {"remediation_source": "none", "agent": {"attempted": False}},
        },
    }
    _, detail, _ = _stage_and_detail_from_live_story(story)
    assert "DROPPED_SHAPE" in detail
    assert "missing_acceptance_criteria" in detail
    assert "no acceptance_criteria section found" in detail
    assert "agent_attempted=no" in detail


def _build_intake_dropped_entry(canonical_ref: str) -> dict:
    """Mirror the dict shape that runner._record_current_story_entry writes for
    a DROPPED_AFTER_FIX story so the audit + summary writers can be tested in
    isolation from the runner."""
    f = _finding("groom_how_shaped_ac", "ACs prescribe implementation steps")
    outcome = IntakeOutcome(
        slug="task-1",
        kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
        findings=(f,),
        detail="rerun gate still failing; did not edit issue",
        audit={
            "remediation_source": "agent",
            "agent": {"attempted": True, "cost_usd": 0.083, "model_used": "claude"},
        },
    )
    summary = _intake_outcome_summary(outcome)
    error_type = _intake_error_type(outcome)
    intake_block = _intake_audit_block(outcome)
    return {
        "path": f"Issue #{canonical_ref.split(':')[1]}",
        "slug": "task-1",
        "outcome": "DROPPED_AFTER_FIX",
        "verdict": None,
        "cost_usd": 0.0,
        "story_run_id": "run-test",
        "preflight": None,
        "preflight_original_verdict": None,
        "preflight_source_run_id": None,
        "error": summary,
        "error_type": error_type,
        "outcome_code": error_type,
        "merge": False,
        "batch": 0,
        "depends_on": [],
        "dependency_warnings": [],
        "inferred_dependencies": {"manifest": [], "github_blockers": []},
        "intake": intake_block,
    }


def _empty_sprint_result() -> SprintResult:
    return SprintResult(
        name="test-sprint",
        specs_total=0,
        specs_succeeded=0,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[],
    )


def _empty_manifest() -> ResolvedSprint:
    return ResolvedSprint(name="test-sprint", budget_usd=10.0, stories=[], max_parallel=1)


def test_audit_yaml_carries_intake_finding_for_dropped_after_fix(tmp_path: Path):
    canonical_ref = "issue:1473"
    entry = _build_intake_dropped_entry(canonical_ref)
    now = datetime.datetime.now(datetime.timezone.utc)

    _write_sprint_audit(
        manifest=_empty_manifest(),
        result=_empty_sprint_result(),
        canonical_refs=[canonical_ref],
        started_at=now,
        finished_at=now,
        duration=0.0,
        project_root=tmp_path,
        current_story_entries_by_ref={canonical_ref: entry},
    )

    audit_yaml = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    spec = audit_yaml["specs"][0]
    # Outcome must be DROPPED_AFTER_FIX, not the silent SKIPPED of today.
    assert spec["outcome"] == "DROPPED_AFTER_FIX"
    # error/error_type must carry the rule code + problem so operators can act
    # without re-deriving with a Python snippet.
    assert "groom_how_shaped_ac" in spec["error"]
    assert "rerun gate still failing" in spec["error"]
    assert spec["error_type"] == "groom_how_shaped_ac"
    assert spec["outcome_code"] == "groom_how_shaped_ac"
    # Structured intake block must round-trip so the full agent attempt
    # context is on disk for postmortem.
    assert spec["intake"]["kind"] == "dropped_after_fix"
    assert spec["intake"]["codes"] == ["groom_how_shaped_ac"]
    assert spec["intake"]["audit"]["agent"]["attempted"] is True
    assert spec["intake"]["audit"]["agent"]["cost_usd"] == 0.083
    assert spec["intake"]["audit"]["agent"]["model_used"] == "claude"
    # Pre-rendered agent_summary string must be on disk too so postmortem
    # tooling does not have to re-derive it from the audit dict.
    assert "agent_attempted=yes" in spec["intake"]["agent_summary"]
    assert "$0.0830" in spec["intake"]["agent_summary"]


def test_sprint_summary_yaml_carries_intake_finding_for_dropped_after_fix(tmp_path: Path):
    canonical_ref = "issue:1473"
    entry = _build_intake_dropped_entry(canonical_ref)
    now = datetime.datetime.now(datetime.timezone.utc)
    log_dir = tmp_path / "logs"

    _write_sprint_summary(
        manifest=_empty_manifest(),
        result=_empty_sprint_result(),
        canonical_refs=[canonical_ref],
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=log_dir,
        current_story_entries_by_ref={canonical_ref: entry},
    )

    summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text())
    story = summary["stories"][0]
    assert story["outcome"] == "DROPPED_AFTER_FIX"
    assert "groom_how_shaped_ac" in story["error"]
    assert story["error_type"] == "groom_how_shaped_ac"
    assert story["outcome_code"] == "groom_how_shaped_ac"
    assert story["intake"]["codes"] == ["groom_how_shaped_ac"]
    assert story["intake"]["findings"][0]["problem"] == "ACs prescribe implementation steps"


def test_live_status_detail_falls_back_when_intake_summary_absent():
    # Backward-compat: a DROPPED_AFTER_FIX entry written by an older path that
    # didn't populate intake_summary still renders something meaningful.
    story = {
        "status": "failed",
        "outcome": "dropped_after_fix",
        "phase": None,
        "blocked_by": [],
        "detail": {"final_outcome": "DROPPED_AFTER_FIX"},
    }
    _, detail, _ = _stage_and_detail_from_live_story(story)
    assert detail == "DROPPED_AFTER_FIX"
