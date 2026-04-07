from __future__ import annotations

from unittest.mock import patch

from tests.coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)
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
@patch("theforge.coordinator.util._run_shell")
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
