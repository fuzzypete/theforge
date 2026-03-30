"""Unit tests for plan_trajectory: theme extraction, anchor-kind overlap, and disposition."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from theforge.coordinator.plan_trajectory import (
    _has_sufficient_overlap,
    _is_file_path_anchor,
    build_disposition_context,
    classify_disposition,
    extract_finding_themes,
    record_plan_attempt,
)
from theforge.coordinator.state import CoordinatorState
from theforge.review import PlanReviewFinding

# ── Helpers ───────────────────────────────────────────────────────────


def _finding(description: str, severity: str = "P1") -> PlanReviewFinding:
    return PlanReviewFinding(severity=severity, description=description, suggestion=None)


def _meta(
    files_touched: int = 0,
    p1_count: int = 0,
    p2_count: int = 0,
    finding_themes: list[str] | None = None,
) -> dict:
    return {
        "files_touched": files_touched,
        "p1_count": p1_count,
        "p2_count": p2_count,
        "finding_themes": finding_themes or [],
    }


# ── Theme extraction ──────────────────────────────────────────────────


def test_snake_case_extracted():
    findings = [_finding("The load_config function is broken")]
    themes = extract_finding_themes(findings)
    assert "load_config" in themes


def test_snake_case_with_leading_underscore():
    findings = [_finding("_validate_plan_provider needs fixing")]
    themes = extract_finding_themes(findings)
    assert "_validate_plan_provider" in themes


def test_snake_case_short_leading_underscore():
    findings = [_finding("_run_plan is missing")]
    themes = extract_finding_themes(findings)
    assert "_run_plan" in themes


def test_camelcase_extracted():
    findings = [_finding("The loadConfig and checkAgentAuth helpers are wrong")]
    themes = extract_finding_themes(findings)
    assert "loadConfig" in themes
    assert "checkAgentAuth" in themes


def test_dotted_path_extracted():
    findings = [_finding("Check config.profiles.dev and state.plan_attempt_metadata")]
    themes = extract_finding_themes(findings)
    assert "config.profiles.dev" in themes
    assert "state.plan_attempt_metadata" in themes


def test_file_path_extracted():
    findings = [_finding("See coordinator.py and src/theforge/config.py for details")]
    themes = extract_finding_themes(findings)
    assert "coordinator.py" in themes
    assert "src/theforge/config.py" in themes


def test_single_segment_words_excluded():
    findings = [_finding("config and auth and schema are wrong")]
    themes = extract_finding_themes(findings)
    assert "config" not in themes
    assert "auth" not in themes
    assert "schema" not in themes


def test_empty_findings_return_empty():
    assert extract_finding_themes([]) == []


def test_empty_description_returns_empty():
    findings = [_finding("")]
    assert extract_finding_themes(findings) == []


def test_deduplication_across_findings():
    findings = [
        _finding("load_config is used here"),
        _finding("load_config is also used here"),
    ]
    themes = extract_finding_themes(findings)
    assert themes.count("load_config") == 1


def test_themes_are_sorted():
    findings = [_finding("load_config and strict_auth and _run_plan")]
    themes = extract_finding_themes(findings)
    assert themes == sorted(themes)


# ── Anchor-kind overlap ───────────────────────────────────────────────


def test_is_file_path_anchor_true():
    assert _is_file_path_anchor("src/theforge/config.py") is True
    assert _is_file_path_anchor("coordinator.py") is True


def test_is_file_path_anchor_false_for_snake():
    assert _is_file_path_anchor("load_config") is False
    assert _is_file_path_anchor("_validate_plan_provider") is False


def test_is_file_path_anchor_false_for_non_code_ext():
    assert _is_file_path_anchor("some.xyz") is False


def test_has_sufficient_overlap_false_when_only_files_overlap():
    prev = ["coordinator.py", "src/theforge/config.py"]
    curr = ["coordinator.py", "src/theforge/config.py"]
    assert _has_sufficient_overlap(prev, curr) is False


def test_has_sufficient_overlap_true_with_identifier_overlap():
    prev = ["coordinator.py", "load_config"]
    curr = ["coordinator.py", "load_config"]
    assert _has_sufficient_overlap(prev, curr) is True


def test_has_sufficient_overlap_true_with_dotted_path():
    prev = ["state.plan_attempt_metadata"]
    curr = ["state.plan_attempt_metadata"]
    assert _has_sufficient_overlap(prev, curr) is True


def test_has_sufficient_overlap_false_no_overlap():
    prev = ["load_config"]
    curr = ["strict_auth"]
    assert _has_sufficient_overlap(prev, curr) is False


# ── Disposition classification ────────────────────────────────────────


def test_single_attempt_returns_patch():
    history = [_meta(files_touched=3, p1_count=2, p2_count=1, finding_themes=["load_config"])]
    assert classify_disposition(history) == "patch"


def test_no_theme_overlap_returns_patch():
    history = [
        _meta(finding_themes=["load_config"], p1_count=2),
        _meta(finding_themes=["strict_auth"], p1_count=1),
    ]
    assert classify_disposition(history) == "patch"


def test_decreasing_finding_count_returns_patch():
    # Even if themes survive, decreasing count → patch (progress)
    history = [
        _meta(p1_count=3, p2_count=2, finding_themes=["load_config", "strict_auth"]),
        _meta(p1_count=1, p2_count=1, finding_themes=["load_config", "strict_auth"]),
    ]
    assert classify_disposition(history) == "patch"


def test_file_path_only_overlap_returns_patch():
    history = [
        _meta(
            files_touched=3,
            p1_count=2,
            p2_count=0,
            finding_themes=["coordinator.py", "config.py"],
        ),
        _meta(
            files_touched=3,
            p1_count=2,
            p2_count=0,
            finding_themes=["coordinator.py", "config.py"],
        ),
    ]
    assert classify_disposition(history) == "patch"


def test_surviving_identifier_themes_and_flat_files_returns_backtrack():
    history = [
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
    ]
    assert classify_disposition(history) == "backtrack"


def test_surviving_themes_and_growing_files_returns_backtrack():
    history = [
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
        _meta(files_touched=5, p1_count=2, p2_count=0, finding_themes=["load_config"]),
    ]
    assert classify_disposition(history) == "backtrack"


def test_escalate_after_backtrack_with_surviving_themes():
    history = [
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
    ]
    assert classify_disposition(history) == "escalate"


def test_empty_themes_returns_patch():
    history = [
        _meta(p1_count=1, finding_themes=[]),
        _meta(p1_count=1, finding_themes=[]),
    ]
    assert classify_disposition(history) == "patch"


# ── record_plan_attempt ────────────────────────────────────────────────


def test_record_plan_attempt_appends_metadata():
    state = CoordinatorState()
    state.plan_structured = {
        "approach": "test",
        "steps": [
            {
                "id": 1,
                "description": "step",
                "files": ["a.py", "b.py"],
                "action": "modify",
                "details": "",
            },
            {
                "id": 2,
                "description": "step2",
                "files": ["b.py", "c.py"],
                "action": "create",
                "details": "",
            },
        ],
    }
    findings = [
        _finding("The load_config function fails"),
        _finding("P2 issue with strict_auth", severity="P2"),
    ]
    record_plan_attempt(state, findings)
    assert len(state.plan_attempt_metadata) == 1
    entry = state.plan_attempt_metadata[0]
    assert entry["files_touched"] == 3  # a.py, b.py, c.py (deduplicated)
    assert entry["p1_count"] == 1
    assert entry["p2_count"] == 1
    assert "load_config" in entry["finding_themes"]
    assert "strict_auth" in entry["finding_themes"]


def test_record_plan_attempt_sets_disposition():
    state = CoordinatorState()
    state.plan_structured = {"approach": "x", "steps": []}
    record_plan_attempt(state, [_finding("load_config issue")])
    assert state.plan_regen_disposition == "patch"  # single attempt


def test_record_plan_attempt_no_plan_structured():
    state = CoordinatorState()
    state.plan_structured = None
    record_plan_attempt(state, [])
    assert state.plan_attempt_metadata[0]["files_touched"] == 0


# ── build_disposition_context ─────────────────────────────────────────


def test_build_disposition_context_empty_for_single_entry():
    state = CoordinatorState()
    state.plan_attempt_metadata = [_meta(p1_count=2)]
    state.plan_regen_disposition = "patch"
    assert build_disposition_context(state) == ""


def test_build_disposition_context_empty_for_no_entries():
    state = CoordinatorState()
    assert build_disposition_context(state) == ""


def test_build_disposition_context_returns_markdown_table():
    state = CoordinatorState()
    state.plan_attempt_metadata = [
        _meta(files_touched=3, p1_count=2, p2_count=1, finding_themes=["load_config"]),
        _meta(files_touched=3, p1_count=2, p2_count=1, finding_themes=["load_config"]),
    ]
    state.plan_regen_disposition = "backtrack"
    ctx = build_disposition_context(state)
    assert "## Trajectory Analysis" in ctx
    assert "**backtrack**" in ctx
    assert "| Attempt |" in ctx
    assert "| 1 |" in ctx
    assert "| 2 |" in ctx


def test_build_disposition_context_patch_guidance():
    state = CoordinatorState()
    state.plan_attempt_metadata = [
        _meta(p1_count=2, finding_themes=["load_config"]),
        _meta(p1_count=1, finding_themes=["strict_auth"]),
    ]
    state.plan_regen_disposition = "patch"
    ctx = build_disposition_context(state)
    assert "Focus on the new findings only" in ctx


def test_build_disposition_context_backtrack_guidance():
    state = CoordinatorState()
    state.plan_attempt_metadata = [
        _meta(p1_count=2, finding_themes=["load_config"]),
        _meta(p1_count=2, finding_themes=["load_config"]),
    ]
    state.plan_regen_disposition = "backtrack"
    ctx = build_disposition_context(state)
    assert "Re-examine your approach" in ctx


def test_build_disposition_context_escalate_guidance():
    state = CoordinatorState()
    state.plan_attempt_metadata = [
        _meta(p1_count=2, finding_themes=["load_config"]),
        _meta(p1_count=2, finding_themes=["load_config"]),
        _meta(p1_count=2, finding_themes=["load_config"]),
    ]
    state.plan_regen_disposition = "escalate"
    ctx = build_disposition_context(state)
    assert "fundamentally different approach" in ctx


# ── Regression tests ──────────────────────────────────────────────────


def test_shrinking_files_touched_returns_patch_not_backtrack():
    """Themes survive but complexity shrank → patch, not backtrack."""
    history = [
        _meta(files_touched=5, p1_count=2, p2_count=0, finding_themes=["load_config"]),
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
    ]
    assert classify_disposition(history) == "patch"


def test_shrinking_files_touched_prevents_escalate():
    """Even after a backtrack, if complexity shrinks on the third attempt → patch."""
    history = [
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
        _meta(files_touched=3, p1_count=2, p2_count=0, finding_themes=["load_config"]),
        _meta(files_touched=2, p1_count=2, p2_count=0, finding_themes=["load_config"]),
    ]
    # Third entry: themes survive but files_touched shrank → patch (not escalate)
    assert classify_disposition(history) == "patch"


# ── Human plan review approve path emits phase_end ────────────────────


def test_human_plan_review_approve_emits_phase_end(tmp_path):
    """Human plan review approve path must emit phase_end with plan_regen_disposition."""
    from theforge.config import (
        DEFAULT_DEV_PROFILE,
        DEFAULT_PREFLIGHT_PROFILE,
        ForgeConfig,
        LogConfig,
        ModelProfile,
        PlanConfig,
        PlanReviewConfig,
        RetryPolicy,
        WorkspaceConfig,
    )
    from theforge.coordinator.plan_flow import _run_human_plan_review
    from theforge.coordinator.state import CoordinatorState, Phase
    from theforge.task import TaskStory

    workspace = tmp_path / "ws"
    workspace.mkdir()
    plan_path = workspace / ".forge" / "plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("# Plan", encoding="utf-8")

    state = CoordinatorState()
    state.phase = Phase.PLAN_REVIEW

    config = ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=None,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig(enabled=True, budget_usd=0.5, timeout=60),
        plan_review=PlanReviewConfig(enabled=True, mode="blocking", timeout_seconds=60),
        log=LogConfig(enabled=False),
    )

    story_file = tmp_path / "story.md"
    story_file.write_text("Do something.", encoding="utf-8")
    task = TaskStory(
        name="test",
        slug="test",
        story_path=story_file,
    )

    plan_profile = ModelProfile(
        name="plan",
        cli="claude",
        model="claude-opus-4",
        provider="anthropic",
        budget_usd=0.5,
        timeout_seconds=60,
        allowed_tools=[],
    )

    mock_logger = MagicMock()
    emitted: list[dict] = []

    def capture_emit(event: str, **fields: object) -> None:
        emitted.append({"event": event, **fields})

    mock_logger._safe_emit.side_effect = capture_emit

    with (
        patch("theforge.coordinator.plan_flow._plan_review_interactive", return_value="approve"),
        patch("theforge.coordinator.plan_flow._is_pending_file_mode", return_value=False),
        patch("theforge.coordinator.plan_flow._is_remote_mode", return_value=False),
        patch("theforge.coordinator.util._run_shell"),
    ):
        result = _run_human_plan_review(
            state=state,
            config=config,
            task=task,
            workspace_path=workspace,
            plan_profile=plan_profile,
            plan_text="# Plan",
            plan_prompt="Make a plan.",
            notify=False,
            logger=mock_logger,
            run_id=None,
        )

    assert result is None  # approved, continues to DEV
    phase_end_events = [
        e for e in emitted if e["event"] == "phase_end" and e.get("phase") == "PLAN_REVIEW"
    ]
    assert len(phase_end_events) >= 1
    approve_event = next((e for e in phase_end_events if e.get("outcome") == "approve"), None)
    assert approve_event is not None, f"No approve phase_end event found in {phase_end_events}"
    assert "plan_regen_disposition" in approve_event
