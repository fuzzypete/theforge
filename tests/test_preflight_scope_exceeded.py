"""Scope-exceeded scoring semantics: 9 is the largest coherent story, 10 is
work that should be decomposed (issue #2680).

Three layers:
  - Prompt contract — the preflight rubric splits 9 and 10 into mutually
    exclusive anchors and asks for a ``scope_exceeded`` boolean.
  - Parser — ``scope_exceeded`` is read strictly, so only a real boolean is a
    claim about scope.
  - Seam + persistence — the signal is derived from the *implementation* axis
    at its ceiling, is readable directly (not inferred from the number), and
    survives the artifact, the audit block, the routing record, the resume
    record, and the preflight cache.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_plan_config,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import LogConfig
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import _parse_preflight_scope_exceeded
from theforge.coordinator.preflight_cache import apply_cached_preflight_state
from theforge.coordinator.routing_persistence import (
    apply_routing_record_to_state,
    build_routing_record,
)
from theforge.coordinator.state import CoordinatorState
from theforge.task import TaskStory, build_preflight_prompt

# ── Prompt contract ───────────────────────────────────────────────────────


def _prompt(tmp_path: Path) -> str:
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n\nDo something.", encoding="utf-8")
    task = TaskStory(name="Story", story_path=spec, slug="test-task")
    return build_preflight_prompt(task, story_content="# Test\n\nDo something.")


def test_prompt_gives_9_and_10_separate_rubrics(tmp_path):
    prompt = _prompt(tmp_path)
    # The merged "9-10 (very large)" anchor is gone.
    assert "9–10 (very large)" not in prompt
    assert "9-10 (very large)" not in prompt
    # Each end of the ceiling states its own, mutually exclusive meaning.
    assert "**9 (very large, still one story)**" in prompt
    assert "**10 (beyond one story)**" in prompt
    assert "Scores 9 and 10 are mutually exclusive." in prompt
    assert "independently landable units" in prompt


def test_prompt_requires_scope_exceeded_in_output_block(tmp_path):
    prompt = _prompt(tmp_path)
    assert "scope_exceeded: true | false" in prompt
    # Tied to the implementation ceiling, and explicitly not to validation load.
    assert "`scope_exceeded: true`" in prompt
    assert "expensive to validate" in prompt


# ── Parser ────────────────────────────────────────────────────────────────


def _yaml_output(body: str) -> str:
    return f"```yaml\n{body}\n```"


def test_parse_scope_exceeded_true():
    out = _yaml_output("verdict: PROCEED\ncomplexity_score: 10\nscope_exceeded: true")
    assert _parse_preflight_scope_exceeded(out) is True


def test_parse_scope_exceeded_false():
    out = _yaml_output("verdict: PROCEED\ncomplexity_score: 4\nscope_exceeded: false")
    assert _parse_preflight_scope_exceeded(out) is False


def test_parse_scope_exceeded_absent_is_none():
    out = _yaml_output("verdict: PROCEED\ncomplexity_score: 4")
    assert _parse_preflight_scope_exceeded(out) is None


def test_parse_scope_exceeded_non_boolean_is_none():
    # A string, a number, or a list is not a claim about scope — never coerced
    # into truthiness, so a malformed emission cannot assert decomposition.
    for raw in ('"yes"', "1", "[true]", "maybe", "null"):
        out = _yaml_output(f"verdict: PROCEED\nscope_exceeded: {raw}")
        assert _parse_preflight_scope_exceeded(out) is None


def test_parse_scope_exceeded_malformed_yaml_is_none():
    assert _parse_preflight_scope_exceeded("not yaml at all: [[[") is None


# ── Seam fixtures ─────────────────────────────────────────────────────────

_STORY_ORDINARY = """\
# Add a debug log line to the config loader

## Acceptance criteria

- A debug log line is emitted when the config file is loaded.
"""

# Issue-1326-shaped: a small code change with a large validation envelope.
_STORY_VALIDATION_HEAVY = """\
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


def _preflight_output(score: int, *, scope_exceeded: str | None = None) -> str:
    scope_line = "" if scope_exceeded is None else f"scope_exceeded: {scope_exceeded}\n"
    return (
        "```yaml\n"
        "verdict: PROCEED\n"
        f"complexity_score: {score}\n"
        f"{scope_line}"
        "complexity: large\n"
        "work_type: feature\n"
        "contract_change: false\n"
        'reason: "Sized for the scope-exceeded seam test."\n'
        "sufficiency: implementation_ready\n"
        'sufficiency_reason: "Bounded single-area change."\n'
        "spec_issues: []\n"
        "warnings: []\n"
        "criteria_checked:\n"
        '  - criterion: "Behavior present"\n'
        "    satisfied: false\n"
        '    evidence: "Not present"\n'
        "```\n"
    )


def _task_with_text(tmp_path: Path, story_text: str) -> TaskStory:
    spec = tmp_path / "spec.md"
    spec.write_text(story_text, encoding="utf-8")
    return TaskStory(name="Story", story_path=spec, slug="test-task", story_text=story_text)


def _run(tmp_path, mocks, *, story_text: str, preflight_output: str):
    """Drive run_task with a stubbed preflight emission; return the result."""
    mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool = mocks
    config = dataclasses.replace(_make_plan_config(tmp_path), log=LogConfig(enabled=True))
    task = _task_with_text(tmp_path, story_text)
    workspace = tmp_path / "test-task"
    workspace.mkdir(exist_ok=True)

    mock_preflight.return_value = _make_agent_result(
        success=True, output=preflight_output, cost_usd=0.05
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
    return run_task(config, task), config, task


class TestScopeExceededSeam:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_implementation_ceiling_records_scope_exceeded(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """An implementation score of 10 is reported as a distinct readable
        signal on state, in the artifact, and in the audit preflight block."""
        result, config, task = _run(
            tmp_path,
            (mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool),
            story_text=_STORY_ORDINARY,
            preflight_output=_preflight_output(10, scope_exceeded="true"),
        )
        state = result.state

        assert result.success is True
        assert state.preflight_implementation_complexity_score == 10
        assert state.preflight_scope_exceeded is True

        artifact = yaml.safe_load((state.log_dir / "preflight.yaml").read_text(encoding="utf-8"))
        assert artifact["scope_exceeded"] is True

        audit = generate_audit_log(config, task, result)
        assert audit["preflight"]["scope_exceeded"] is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_validation_lift_does_not_report_scope_exceeded(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """A cohesive story with a large validation envelope keeps
        scope_exceeded false even though its projected complexity_score is
        lifted well above the implementation score."""
        result, _config, _task = _run(
            tmp_path,
            (mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool),
            story_text=_STORY_VALIDATION_HEAVY,
            preflight_output=_preflight_output(4, scope_exceeded="false"),
        )
        state = result.state

        assert result.success is True
        assert state.preflight_implementation_complexity_score == 4
        # The validation envelope lifted the projected score.
        assert state.preflight_complexity_score > 4
        assert state.preflight_complexity_score == state.preflight_validation_complexity_score
        # …but the story is one coherent unit, so it is not over scope.
        assert state.preflight_scope_exceeded is False

        artifact = yaml.safe_load((state.log_dir / "preflight.yaml").read_text(encoding="utf-8"))
        assert artifact["scope_exceeded"] is False

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_claimed_scope_exceeded_below_ceiling_is_warned_not_obeyed(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """A classifier that claims scope_exceeded while scoring below the
        implementation ceiling contradicted itself. Recorded decision: the
        score is authoritative, the claim becomes a preflight warning."""
        result, _config, _task = _run(
            tmp_path,
            (mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool),
            story_text=_STORY_ORDINARY,
            preflight_output=_preflight_output(7, scope_exceeded="true"),
        )
        state = result.state

        assert result.success is True
        assert state.preflight_implementation_complexity_score == 7
        assert state.preflight_scope_exceeded is False
        assert any(
            "scope_exceeded=True" in w and "complexity_score=7" in w
            for w in state.preflight_warnings
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_scope_exceeded_survives_the_resume_record(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """A resumed attempt allocates a fresh state; the signal must come back
        with the rest of the preflight judgement rather than silently reset."""
        from theforge.coordinator.resume_persistence import (
            apply_resume_record_to_state,
            load_resume_record,
        )

        result, config, _task = _run(
            tmp_path,
            (mock_shell, mock_dev_agent, mock_preflight, mock_plan_agent, mock_pool),
            story_text=_STORY_ORDINARY,
            preflight_output=_preflight_output(10, scope_exceeded="true"),
        )
        assert result.state.preflight_scope_exceeded is True

        record = load_resume_record(config.project_root, "test-task")
        assert record is not None
        assert record["preflight"]["scope_exceeded"] is True

        fresh = CoordinatorState()
        apply_resume_record_to_state(fresh, record)
        assert fresh.preflight_scope_exceeded is True


# ── Persistence unit coverage ─────────────────────────────────────────────


def test_routing_record_carries_scope_exceeded_both_ways():
    state = CoordinatorState()
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 10
    state.preflight_implementation_complexity_score = 10
    state.preflight_scope_exceeded = True
    record = build_routing_record(
        state=state,
        slug="test-task",
        run_id="run-1",
        story_content="# Story",
        dev_model="dev",
        plan_model="plan",
        review_pool_models=["review"],
    )
    assert record["scope_exceeded"] is True

    restored = CoordinatorState()
    apply_routing_record_to_state(restored, record)
    assert restored.preflight_scope_exceeded is True


def test_routing_record_without_the_signal_leaves_state_default():
    restored = CoordinatorState()
    apply_routing_record_to_state(restored, {"complexity": "medium", "complexity_score": 5})
    assert restored.preflight_scope_exceeded is False


def test_cached_preflight_copies_scope_exceeded():
    cached = CoordinatorState()
    cached.preflight_verdict = "PROCEED"
    cached.preflight_complexity = "large"
    cached.preflight_complexity_score = 10
    cached.preflight_implementation_complexity_score = 10
    cached.preflight_scope_exceeded = True

    live = CoordinatorState()
    apply_cached_preflight_state(live, cached)
    assert live.preflight_scope_exceeded is True


def test_state_default_is_false():
    assert CoordinatorState().preflight_scope_exceeded is False
