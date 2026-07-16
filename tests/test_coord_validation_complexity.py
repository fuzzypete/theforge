"""Validation-complexity sizing: separate implementation vs validation envelopes
with cited structural evidence and a projected legacy complexity_score (issue #1442).

Two layers:
  - Unit tests for the pure structural rule engine in
    ``theforge.coordinator.validation_complexity`` — surface counting, verb-object
    cost-bearing detection, release-gate conjunction, parsed dependency fan-in,
    projection, and negative (non-keyword) discrimination.
  - Seam tests that drive ``run_task`` end-to-end and assert both native scores,
    the projection, and cited evidence land on coordinator state and the
    preflight.yaml artifact.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_plan_config,
    _shell_with_gate,
)

from theforge.config import LogConfig
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorState
from theforge.coordinator.validation_complexity import (
    PROJECTION_MAX_IMPL_VALIDATION,
    assess_validation_complexity,
    implementation_evidence,
    project_complexity_score,
)
from theforge.task import TaskStory

# ── Fixtures ──────────────────────────────────────────────────────────────

# An issue-1326-shaped story: many operator-visible surfaces enumerated in the
# AC, a real cost-bearing recursive invocation, release-gate language, and a
# multi-issue dependency fan-in.
_STORY_1326 = """\
# Validate v0.11.0 substrate surfaces end-to-end

Depends on #792, #793, #1101, #1324, #1325 — all scheduled in the same release.

## Acceptance criteria

- Run a real `forge sprint` against TheForge and confirm the story lands.
- `audit show` renders the completed run.
- `forge check-config` passes against the populated substrate.
- Resuming an interrupted sprint with `--resume` continues from the last phase.
- `history.jsonl` is imported one-shot and `sprint-rca.yaml` is rendered.
- This checklist gates the v0.11.0 release; the release is blocked until it passes.
"""

# An ordinary tight-AC code-change story with no validation envelope.
_STORY_ORDINARY = """\
# Add a debug log line to the config loader

## Acceptance criteria

- A debug log line is emitted when the config file is loaded.
"""


# ── Unit: full envelope ────────────────────────────────────────────────────


def test_issue_1326_shaped_story_scores_high_validation():
    result = assess_validation_complexity(_STORY_1326)
    assert result.score >= 8
    fired = {e.rule_id for e in result.evidence}
    assert fired == {
        "validation_surface_count",
        "cost_bearing_dogfood",
        "release_blocking_validation",
        "dependency_fan_in",
    }
    # Every evidence entry carries a rule_id + non-empty signal on the validation axis.
    for e in result.evidence:
        assert e.dimension == "validation"
        assert e.rule_id and e.signal
    # Warnings surface the envelope to operators.
    assert any("operator-visible surfaces" in w for w in result.warnings)
    assert "release-blocking dogfood validation" in result.warnings


def test_ordinary_story_scores_baseline_validation():
    result = assess_validation_complexity(_STORY_ORDINARY)
    assert result.score == 1
    assert result.evidence == []
    assert result.warnings == []


def test_empty_body_is_baseline():
    result = assess_validation_complexity("")
    assert result.score == 1
    assert result.evidence == []


# ── Unit: surface counting ─────────────────────────────────────────────────


def test_surface_count_fires_on_many_surfaces():
    body = (
        "## Acceptance criteria\n"
        "- `audit show` renders it.\n"
        "- `forge check-config` passes.\n"
        "- pass `--resume` to continue.\n"
        "- `history.jsonl` imported and `sprint-rca.yaml` rendered.\n"
    )
    result = assess_validation_complexity(body)
    ids = {e.rule_id for e in result.evidence}
    assert "validation_surface_count" in ids


def test_surface_count_does_not_fire_on_two_surfaces():
    body = "## Acceptance criteria\n- `audit show` renders `run.yaml`.\n"
    result = assess_validation_complexity(body)
    ids = {e.rule_id for e in result.evidence}
    assert "validation_surface_count" not in ids


def test_surface_count_uses_acceptance_region_not_background():
    # Surfaces mentioned only in background prose (no AC heading section) must not
    # inflate the count when an acceptance-criteria section exists with few surfaces.
    body = (
        "# Story\n\n"
        "Background mentions `audit show`, `forge check-config`, `--resume`, "
        "`history.jsonl`, and `sprint-rca.yaml` for context.\n\n"
        "## Acceptance criteria\n"
        "- The loader emits one debug line.\n"
    )
    result = assess_validation_complexity(body)
    ids = {e.rule_id for e in result.evidence}
    assert "validation_surface_count" not in ids


# ── Unit: cost-bearing verb-object recognition ─────────────────────────────


def test_cost_bearing_fires_on_directive_plus_command():
    body = "## Acceptance criteria\n- Run a real `forge sprint` and confirm it lands.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "cost_bearing_dogfood" in ids


def test_cost_bearing_does_not_fire_without_directive_verb():
    # Mentioning the command as a noun ("the forge sprint output format") is not a
    # directive to invoke it.
    body = "## Acceptance criteria\n- Document the forge sprint output format.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "cost_bearing_dogfood" not in ids


def test_cost_bearing_does_not_fire_for_test_fixture():
    body = "## Acceptance criteria\n- Run a mocked `forge sprint` as a test fixture.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "cost_bearing_dogfood" not in ids


# ── Unit: release-gate conjunction ─────────────────────────────────────────


def test_release_blocking_fires_on_version_plus_gate():
    body = "## Acceptance criteria\n- This gates the v0.11.0 release.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "release_blocking_validation" in ids


def test_release_blocking_fires_on_until_passes_clause():
    body = "## Acceptance criteria\n- The release is blocked until the checklist passes.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "release_blocking_validation" in ids


def test_release_blocking_does_not_fire_on_bare_version():
    body = "## Acceptance criteria\n- v0.11.0 adds a new config flag.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "release_blocking_validation" not in ids


def test_release_blocking_does_not_fire_on_bare_block_word():
    # "block" without a release/version context is a false-positive trap.
    body = "## Acceptance criteria\n- Add a config block to the loader.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "release_blocking_validation" not in ids


# ── Unit: dependency fan-in parsing ────────────────────────────────────────


def test_dependency_fan_in_fires_on_parsed_list():
    body = "Depends on #101, #202, #303.\n\n## Acceptance criteria\n- Do the thing.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "dependency_fan_in" in ids


def test_dependency_fan_in_ignores_refs_without_dep_context():
    # Issue references outside a dependency-declaring phrase are background, not deps.
    body = "See #101 and #202 for background.\n\n## Acceptance criteria\n- Do it.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "dependency_fan_in" not in ids


def test_dependency_fan_in_needs_two_distinct():
    body = "Depends on #101.\n\n## Acceptance criteria\n- Do it.\n"
    ids = {e.rule_id for e in assess_validation_complexity(body).evidence}
    assert "dependency_fan_in" not in ids


# ── Unit: projection ───────────────────────────────────────────────────────


def test_projection_takes_max():
    score, rule = project_complexity_score(4, 9)
    assert score == 9
    assert rule == PROJECTION_MAX_IMPL_VALIDATION


def test_projection_unchanged_when_validation_low():
    score, rule = project_complexity_score(7, 1)
    assert score == 7
    assert rule == PROJECTION_MAX_IMPL_VALIDATION


def test_projection_handles_missing_implementation():
    score, _ = project_complexity_score(None, 5)
    assert score == 5


def test_projection_clamps_to_bounds():
    assert project_complexity_score(20, 3)[0] == 10
    assert project_complexity_score(0, 0)[0] == 1


def test_implementation_evidence_records_source():
    ev = implementation_evidence(4, large_categories=["concurrency control"], contract_change=True)
    ids = {e.rule_id for e in ev}
    assert "implementation_model_score" in ids
    assert "implementation_contract_change" in ids
    assert "implementation_large_category" in ids
    assert all(e.dimension == "implementation" for e in ev)


def test_implementation_evidence_agent_failure():
    ev = implementation_evidence(9, agent_failed=True)
    assert {e.rule_id for e in ev} == {"implementation_agent_failure"}


# ── Seam: run_task propagates dual scores + evidence to state and artifact ──

_PREFLIGHT_1326 = """\
```yaml
verdict: PROCEED
complexity_score: 4
complexity: medium
work_type: feature
contract_change: false
reason: "Small code change but a large validation envelope."
sufficiency: needs_planning
sufficiency_reason: "Multiple surfaces must be verified against a real sprint."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Surfaces verified"
    satisfied: false
    evidence: "Not yet run"
```
"""

_PREFLIGHT_ORDINARY = """\
```yaml
verdict: PROCEED
complexity_score: 3
complexity: small
work_type: feature
contract_change: false
reason: "Localized log line addition."
sufficiency: implementation_ready
sufficiency_reason: "Bounded single-area change."
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Log line emitted"
    satisfied: false
    evidence: "Not present"
```
"""


def _task_with_text(tmp_path: Path, story_text: str) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text(story_text, encoding="utf-8")
    return TaskStory(name="Story", story_path=spec, slug="test-task", story_text=story_text)


class TestDualScoreSeam:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_high_validation_story_lifts_projected_complexity(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        config = dataclasses.replace(_make_plan_config(tmp_path), log=LogConfig(enabled=True))
        task = _task_with_text(tmp_path, _STORY_1326)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_1326, cost_usd=0.05
        )
        plan_result = _make_agent_result(success=True, output="# Plan\n\nStep 1.", cost_usd=0.10)
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)
        call_idx = {"n": 0}
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        state = result.state

        assert result.success is True
        # Native axes present and distinct.
        assert state.preflight_implementation_complexity_score == 4
        assert state.preflight_validation_complexity_score >= 8
        # Legacy score is projected as the max — the validation lift wins.
        assert state.preflight_complexity_score == state.preflight_validation_complexity_score
        assert state.preflight_complexity == "large"
        assert state.preflight_complexity_projection == PROJECTION_MAX_IMPL_VALIDATION
        # Cited evidence spans both axes and includes the validation rules.
        fired = {e["rule_id"] for e in state.preflight_complexity_evidence}
        assert "implementation_model_score" in fired
        assert {
            "validation_surface_count",
            "cost_bearing_dogfood",
            "release_blocking_validation",
            "dependency_fan_in",
        } <= fired

        # Artifact carries the new fields.
        import yaml as _yaml

        artifact = _yaml.safe_load((state.log_dir / "preflight.yaml").read_text(encoding="utf-8"))
        assert artifact["implementation_complexity_score"] == 4
        assert artifact["validation_complexity_score"] >= 8
        assert artifact["complexity_projection"] == PROJECTION_MAX_IMPL_VALIDATION
        assert artifact["complexity_evidence"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_ordinary_story_projection_equals_implementation(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        config = _make_plan_config(tmp_path)
        task = _task_with_text(tmp_path, _STORY_ORDINARY)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_ORDINARY, cost_usd=0.05
        )
        mock_dev_agent.return_value = _make_agent_result(
            success=True, output="Done.", cost_usd=0.50
        )
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        state = result.state

        assert result.success is True
        assert state.preflight_implementation_complexity_score == 3
        assert state.preflight_validation_complexity_score == 1
        # Projection leaves the legacy score equivalent to today's behavior.
        assert state.preflight_complexity_score == 3
        assert state.preflight_complexity == "small"
        # Plan skipped for the ordinary bounded story.
        assert mock_plan_agent.call_count == 0

    @patch("theforge.coordinator.preflight_flow._has_prior_execution_evidence", return_value=True)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_ambiguity_downgrade_keeps_dual_axis_consistent(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        _mock_prior,
        tmp_path,
    ):
        """An ambiguity BLOCKED→PROCEED downgrade force-bumps complexity_score; the
        projection, both native axes, and cited evidence must stay consistent with
        the bumped value rather than going stale (issue #1442 review finding)."""
        config = _make_plan_config(tmp_path)
        task = _task_with_text(tmp_path, _STORY_ORDINARY)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        blocked_ambiguous = """\
```yaml
verdict: BLOCKED
reason: "Acceptance criteria are ambiguous and not objectively verifiable."
complexity: small
complexity_score: 2
work_type: feature
sufficiency: needs_planning
spec_issues: []
warnings: []
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Cannot verify without external API"
```
"""
        mock_preflight.return_value = _make_agent_result(
            success=True, output=blocked_ambiguous, cost_usd=0.05
        )
        plan_result = _make_agent_result(success=True, output="# Plan\n\nStep 1.", cost_usd=0.10)
        dev_result = _make_agent_result(success=True, output="Done.", cost_usd=0.50)
        call_idx = {"n": 0}
        results = [plan_result, dev_result]

        def agent_side_effect(**kwargs):
            idx = min(call_idx["n"], len(results) - 1)
            call_idx["n"] += 1
            return results[idx]

        mock_plan_agent.side_effect = mock_dev_agent
        mock_dev_agent.side_effect = agent_side_effect
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        state = result.state

        assert result.success is True
        assert state.preflight_degraded_reason == "blocked_downgraded_prior_evidence"
        # The force-bump raised the implementation floor and re-projected.
        assert state.preflight_complexity == "medium"
        assert state.preflight_implementation_complexity_score == 5
        assert state.preflight_validation_complexity_score == 1
        # complexity_score must equal the re-projected max, not a stale value.
        projected, rule = project_complexity_score(
            state.preflight_implementation_complexity_score,
            state.preflight_validation_complexity_score,
        )
        assert state.preflight_complexity_score == projected == 5
        assert state.preflight_complexity_projection == rule == PROJECTION_MAX_IMPL_VALIDATION
        # Evidence records the override so the derivation stays auditable.
        fired = {e["rule_id"] for e in state.preflight_complexity_evidence}
        assert "implementation_ambiguity_downgrade_floor" in fired


def test_state_defaults_are_present():
    state = CoordinatorState()
    assert state.preflight_implementation_complexity_score is None
    assert state.preflight_validation_complexity_score is None
    assert state.preflight_complexity_projection is None
    assert state.preflight_complexity_evidence == []
