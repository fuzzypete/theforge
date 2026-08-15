from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import yaml
from coord_test_helpers import patch_gate_shell

from tests.coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_config,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)
from theforge.config.types import KnowledgeConfig
from theforge.coordinator.engine import run_task
from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.state import CoordinatorState, ReviewCycleMetadata
from theforge.task.context_assembler import ContextPack


class _AssemblerSpy:
    calls: list[dict] = []

    @classmethod
    def from_config(cls, _config):
        return cls()

    def assemble(self, **kwargs):
        type(self).calls.append(kwargs)
        return ContextPack(
            content="",
            included=(),
            dropped=(),
            budget=kwargs.get("budget", 0) or 0,
            line_count=0,
            phase=kwargs["phase"],
            structural_index_git_sha=None,
        )


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_review_pool_uses_plan_file_list_for_context(mock_pool, _mock_log, tmp_path):
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = CoordinatorState(
        review_cycle=0,
        log_dir=tmp_path / "logs",
        preflight_likely_files=["wrong/preflight.py"],
        plan_structured={
            "steps": [
                {"files": ["src/right.py", "src/shared.py"]},
                {"files": ["src/shared.py", "tests/test_right.py"]},
            ]
        },
    )
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]
    _AssemblerSpy.calls = []

    with patch("theforge.coordinator.review_pool.ContextAssembler", _AssemblerSpy, create=True):
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
        )

    assert _AssemblerSpy.calls[0]["phase"] == "review"
    assert _AssemblerSpy.calls[0]["file_list"] == [
        "src/right.py",
        "src/shared.py",
        "tests/test_right.py",
    ]


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_run_task_invokes_context_assembler_for_all_phases(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True,
        output=(
            "```yaml\n"
            "verdict: PROCEED\n"
            "complexity: medium\n"
            "reason: planning needed\n"
            "likely_files:\n"
            "  - src/preflight_scope.py\n"
            "  - tests/test_preflight_scope.py\n"
            "criteria_checked:\n"
            "  - criterion: Feature X\n"
            "    satisfied: false\n"
            "    evidence: Not found in codebase\n"
            "```\n"
        ),
        profile_name="preflight",
    )
    plan_output = (
        "```yaml\n"
        "plan:\n"
        "  approach: Update app behavior and coverage.\n"
        "  steps:\n"
        "    - id: 1\n"
        "      description: Update code\n"
        "      files:\n"
        "        - src/app.py\n"
        "        - tests/test_app.py\n"
        "      action: modify\n"
        "      details: Implement the feature.\n"
        "    - id: 2\n"
        "      description: Follow-up\n"
        "      files:\n"
        "        - src/app.py\n"
        "        - docs/notes.md\n"
        "      action: modify\n"
        "      details: Document the change.\n"
        "```\n"
    )
    mock_plan.return_value = _make_agent_result(
        success=True,
        output=plan_output,
        profile_name="plan",
    )
    mock_dev.return_value = _make_agent_result(success=True, output="Done.", profile_name="dev")
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]
    _AssemblerSpy.calls = []

    with (
        patch("theforge.coordinator.preflight_flow.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.plan_flow.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.dev_phase.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.review_pool.ContextAssembler", _AssemblerSpy, create=True),
    ):
        result = run_task(config, task)

    assert result.success is True
    assert [call["phase"] for call in _AssemblerSpy.calls] == [
        "preflight",
        "plan",
        "dev",
        "review",
    ]
    assert "file_list" not in _AssemblerSpy.calls[0]
    assert _AssemblerSpy.calls[1]["file_list"] == [
        "src/preflight_scope.py",
        "tests/test_preflight_scope.py",
    ]
    assert _AssemblerSpy.calls[2]["file_list"] == [
        "src/app.py",
        "tests/test_app.py",
        "docs/notes.md",
    ]
    assert _AssemblerSpy.calls[3]["file_list"] == [
        "src/app.py",
        "tests/test_app.py",
        "docs/notes.md",
    ]


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_prose_prefixed_plan_reaches_dev_file_count_and_scaling_log(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True,
        output=PREFLIGHT_PROCEED_MEDIUM,
        profile_name="preflight",
    )
    mock_plan.return_value = _make_agent_result(
        success=True,
        output=(
            "I checked the codebase and the structured plan is below.\n\n"
            "```yaml\n"
            "plan:\n"
            "  approach: Update code and tests together.\n"
            "  steps:\n"
            "    - id: 1\n"
            "      description: Update implementation\n"
            "      files:\n"
            "        - src/app.py\n"
            "      action: modify\n"
            "      details: Apply the behavior change.\n"
            "    - id: 2\n"
            "      description: Add coverage\n"
            "      files:\n"
            "        - tests/test_app.py\n"
            "        - src/app.py\n"
            "      action: modify\n"
            "      details: Verify the changed behavior.\n"
            "      depends_on: [1]\n"
            "```\n"
        ),
        profile_name="plan",
    )
    mock_dev.return_value = _make_agent_result(success=True, output="Done.", profile_name="dev")
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    verbose_messages: list[str] = []
    _AssemblerSpy.calls = []

    with (
        patch("theforge.coordinator.preflight_flow.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.plan_flow.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.dev_phase.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.review_pool.ContextAssembler", _AssemblerSpy, create=True),
        patch(
            "theforge.coordinator.dev_phase._log_verbose",
            side_effect=lambda msg: verbose_messages.append(msg),
        ),
    ):
        result = run_task(config, task)

    assert result.success is True
    assert _AssemblerSpy.calls[2]["phase"] == "dev"
    assert _AssemblerSpy.calls[2]["file_list"] == ["src/app.py", "tests/test_app.py"]
    assert any(
        "Stuck-detection scaled for medium (2 plan files)" in msg for msg in verbose_messages
    )


def _write_prior_run_corpus(root, run_id="4f2a91c"):
    """Write an admissible summary whose deterministic fields match the dev file list."""
    index = root / ".forge" / "knowledge" / "index.yaml"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "source_count": 1,
                "indexed_count": 1,
                "skipped_count": 0,
                "entries": [
                    {
                        "run_id": run_id,
                        "generated_at": "2026-08-01T00:00:00",
                        "story": {
                            "slug": "implement-thing",
                            "name": "Implement the thing",
                            "github_issue": 1,
                        },
                        "story_shape": {"work_type": "feature", "complexity": "medium"},
                        "domains": ["backend"],
                        "changed_files": ["src/app.py"],
                        "learned_patterns": [],
                        "summary_path": f".forge/knowledge/summaries/{run_id}.yaml",
                        "admissibility_verdict": {"status": "admissible", "rank": "full"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    summary = root / ".forge" / "knowledge" / "summaries" / f"{run_id}.yaml"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "run_id": run_id,
                "what_changed": {
                    "description": "prior run reworked the thing",
                    "approach": "extracted a helper",
                },
                "what_was_learned": [{"claim": "the thing needs a guard", "evidence": []}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_prior_run_context_flows_through_phase_seams_into_audit_state(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """The config gate must survive the whole phase flow, not just the assembler.

    Each phase records the ContextPack it actually used, so an operator reading
    the audit sees the prior-run decision per phase — including that preflight
    was never offered prior knowledge (ADR-0002 clause 5).
    """
    config = replace(
        _make_plan_config(tmp_path),
        knowledge=KnowledgeConfig(prior_run_context=True),
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()
    _write_prior_run_corpus(tmp_path)

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, profile_name="preflight"
    )
    mock_plan.return_value = _make_agent_result(
        success=True,
        output=(
            "```yaml\n"
            "plan:\n"
            "  approach: Update the thing.\n"
            "  steps:\n"
            "    - id: 1\n"
            "      description: Update implementation\n"
            "      files:\n"
            "        - src/app.py\n"
            "      action: modify\n"
            "      details: Apply the behavior change.\n"
            "```\n"
        ),
        profile_name="plan",
    )
    mock_dev.return_value = _make_agent_result(success=True, output="Done.", profile_name="dev")
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    captured: dict[str, ContextPack] = {}
    result = run_task(config, task)

    assert result.success is True
    for entry in result.state.context_manifests:
        captured.setdefault(entry["phase"], entry["manifest"])

    assert captured["preflight"].prior_run_context["enabled"] is True
    assert captured["preflight"].prior_run_context["included"] == []
    assert "not injected in the preflight phase" in captured["preflight"].prior_run_context["note"]

    dev_prior = captured["dev"].prior_run_context
    assert dev_prior["enabled"] is True
    assert [item["run_id"] for item in dev_prior["included"]] == ["4f2a91c"]
    assert "file_overlap(src/app.py)" in dev_prior["included"][0]["reason"]
    assert "prior run reworked the thing" in captured["dev"].content
