"""Seam tests: forge.yaml sandbox profile → dev runner → audit record (#1947).

Covers the cross-phase flow the capability profile depends on — config carries a
preset name, the dev phase stamps it onto the profile the runner receives, and
the resolved grants land in the audit trail. Unit tests on either end cannot
catch a break in the middle of that chain.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    _write_handoff,
    patch_gate_shell,
)

from theforge.config.sandbox_capabilities import resolve_capabilities
from theforge.config.types import SandboxConfig
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task

_DEFAULT_PAYLOAD = {"profile": None, "write_roots": [], "mach_services": []}


def _run(tmp_path: Path, mocks: dict, *, sandbox: SandboxConfig | None = None):
    workspace = tmp_path / "test-task"
    workspace.mkdir()
    _write_handoff(workspace)

    config = _make_config(tmp_path)
    if sandbox is not None:
        config = dataclasses.replace(config, sandbox=sandbox)
    task = _make_task(tmp_path)

    mocks["shell"].side_effect = _shell_with_gate(workspace)
    mocks["dev"].return_value = _make_agent_result()
    mocks["preflight"].return_value = _PREFLIGHT_RESULT
    mocks["pool"].return_value = [_make_agent_result(output=APPROVE_REVIEW)]

    return config, task, run_task(config, task)


class TestCapabilityProfileReachesRunnerAndAudit:
    @patch("theforge.coordinator.dev_phase.sandbox_containment_mode", return_value="mechanical")
    @patch("theforge.coordinator.dev_phase.sandbox_available_for_profile", return_value=True)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_no_profile_records_explicit_default(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_probe,
        mock_mode,
        tmp_path: Path,
    ) -> None:
        """Default containment is recorded, not omitted, and never widens anything."""
        mocks = {
            "shell": mock_shell,
            "dev": mock_dev,
            "preflight": mock_preflight,
            "pool": mock_pool,
        }
        config, task, result = _run(tmp_path, mocks)

        assert config.sandbox.capability_profile is None
        assert result.state.dev_sandbox_capabilities == _DEFAULT_PAYLOAD
        # The runner sees no capability profile — today's behaviour, unchanged.
        assert mock_dev.call_args.kwargs["profile"].sandbox_capability_profile is None

        log = generate_audit_log(config, task, result)
        assert log["workspace"]["sandbox_capabilities"] == _DEFAULT_PAYLOAD
        for iteration in log["iterations"]["dev_loop"]:
            assert iteration["sandbox_capabilities"] == _DEFAULT_PAYLOAD

    @patch("theforge.coordinator.dev_phase.platform.system", return_value="Darwin")
    @patch("theforge.coordinator.dev_phase.sandbox_containment_mode", return_value="mechanical")
    @patch("theforge.coordinator.dev_phase.sandbox_available_for_profile", return_value=True)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_declared_profile_reaches_runner_and_audit(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_probe,
        mock_mode,
        mock_system,
        tmp_path: Path,
    ) -> None:
        mocks = {
            "shell": mock_shell,
            "dev": mock_dev,
            "preflight": mock_preflight,
            "pool": mock_pool,
        }
        config, task, result = _run(
            tmp_path, mocks, sandbox=SandboxConfig(capability_profile="xcode")
        )

        # Seam 1: config → the profile the runner is actually invoked with.
        assert mock_dev.call_args.kwargs["profile"].sandbox_capability_profile == "xcode"

        # Seam 2: resolved grants → state → audit, matching the declared preset.
        expected = resolve_capabilities("xcode").audit_payload()
        assert result.state.dev_sandbox_capabilities == expected

        log = generate_audit_log(config, task, result)
        assert log["workspace"]["sandbox_capabilities"] == expected
        assert log["workspace"]["sandbox_capabilities"]["profile"] == "xcode"
        for iteration in log["iterations"]["dev_loop"]:
            assert iteration["sandbox_capabilities"] == expected

    @patch("theforge.coordinator.dev_phase.platform.system", return_value="Linux")
    @patch("theforge.coordinator.dev_phase.sandbox_containment_mode", return_value="mechanical")
    @patch("theforge.coordinator.dev_phase.sandbox_available_for_profile", return_value=True)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_unsupported_platform_audits_zero_grants_not_a_silent_widening(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        mock_probe,
        mock_mode,
        mock_system,
        tmp_path: Path,
    ) -> None:
        """A preset this host cannot express must never audit as granted.

        The runner refuses the run, so the audit records empty grants plus the
        declared name and reason — diagnosable, and not a claim of capability
        that was never applied.
        """
        mocks = {
            "shell": mock_shell,
            "dev": mock_dev,
            "preflight": mock_preflight,
            "pool": mock_pool,
        }
        config, task, result = _run(
            tmp_path, mocks, sandbox=SandboxConfig(capability_profile="xcode")
        )

        recorded = result.state.dev_sandbox_capabilities
        assert recorded["profile"] is None
        assert recorded["write_roots"] == []
        assert recorded["mach_services"] == []
        assert recorded["requested_profile"] == "xcode"
        assert "not supported on Linux" in recorded["unsupported_reason"]

        log = generate_audit_log(config, task, result)
        assert log["workspace"]["sandbox_capabilities"]["requested_profile"] == "xcode"
