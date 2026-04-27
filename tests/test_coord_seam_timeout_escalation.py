"""Seam-level integration test for timeout escalation across the DEV→VALIDATE→DEV boundary.

Drives a minimal coordinator run end-to-end: mocked runner exits -9 once, asserting
the second DEV invocation uses the upgraded model+timeout and that the audit YAML
contains the escalation record.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import (  # noqa: E402
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (  # noqa: E402
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log  # noqa: E402
from theforge.coordinator.engine import run_task  # noqa: E402
from theforge.runners import AgentResult  # noqa: E402


def _make_seam_config(tmp_path: Path) -> ForgeConfig:
    dev_profile = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=30.0,
        timeout_seconds=900,
        timeout_medium_seconds=1200,
        timeout_large_seconds=1800,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )
    review_profile = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=10.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=dev_profile,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[review_profile],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=3,
            max_review_cycles=2,
            auto_model_escalation=True,
        ),
        models=["claude/sonnet", "claude/opus"],
    )


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch("theforge.coordinator.util._run_shell")
def test_timeout_escalation_seam(mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path):
    """Full DEV→VALIDATE→DEV seam: timeout on first dev, escalated model on second dev.

    Asserts:
    - Second DEV invocation runs after escalation (run continues rather than halting)
    - state.timeout_escalation_audit is populated with original/new model and timeouts
    - generate_audit_log includes timeout_escalation with the escalation details
    """
    config = _make_seam_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    # Gate: FAIL (dev timed out, no progress), then PASS (escalated dev succeeded)
    mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])

    dev_calls: list[dict] = []

    def dev_side_effect(**kwargs):
        dev_calls.append({"profile": kwargs.get("profile")})
        if len(dev_calls) == 1:
            # First call: timeout
            return AgentResult(
                success=False,
                output="Partial work.",
                session_id="sess-1",
                cost_usd=0.10,
                exit_code=-9,
                raw={},
                profile_name="dev",
            )
        # Second call: escalated model succeeds
        return _make_agent_result(success=True, output="Done.", session_id="sess-1")

    mock_preflight.return_value = _PREFLIGHT_RESULT
    mock_dev.side_effect = dev_side_effect
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
    ]

    result = run_task(config, task)

    # ── Boundary: run completes successfully after escalation ──────────
    assert result.success is True, f"Expected success, got: {result.message}"

    # ── Boundary: two DEV invocations occurred ─────────────────────────
    assert len(dev_calls) == 2, f"Expected 2 dev calls, got {len(dev_calls)}"

    # ── Boundary: escalation state fields set correctly ────────────────
    assert result.state.timeout_escalation_used is True
    assert result.state.timeout_escalation_audit is not None
    audit_state = result.state.timeout_escalation_audit
    assert audit_state["original_model"] == "sonnet"
    assert audit_state["new_model"] == "opus"
    assert audit_state["original_timeout_seconds"] > 0
    assert audit_state["new_timeout_seconds"] > 0
    assert audit_state["reason"] == "timeout"

    # ── Boundary: audit YAML records the escalation ────────────────────
    audit_log = generate_audit_log(config, task, result)
    assert "timeout_escalation" in audit_log
    te = audit_log["timeout_escalation"]
    assert te is not None
    assert te["original_model"] == "sonnet"
    assert te["new_model"] == "opus"
    assert te["reason"] == "timeout"
    assert "original_timeout_seconds" in te
    assert "new_timeout_seconds" in te
