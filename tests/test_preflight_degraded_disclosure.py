"""Regression coverage for the preflight no-delegation guard and the
degraded-preflight disclosure path (#2346).

Two properties are under test:

* **Preflight cannot delegate.** The classifier is denied the one tool that can
  start work it cannot be resumed for. This is a tool-surface guarantee, not a
  prompt one — the prompt rule is belt-and-suspenders and is asserted
  separately.
* **A degraded preflight is disclosed.** A run that proceeded on a conservative
  fallback says so on its own summary row and in the operator-facing digest,
  including when every story reached DONE — the case that hid this defect for
  four months.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
)
from theforge.config.types import ModelProfile
from theforge.coordinator.engine import run_task
from theforge.runners import AgentResult
from theforge.task import build_preflight_prompt

PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Needs implementation."
complexity: medium
sufficiency: needs_planning
work_type: feature
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found"
```
"""


def _crashed_with_salvaged_output() -> AgentResult:
    """A killed preflight that still left a tool trace behind.

    Matches the real failure: a handful of tool calls, no verdict, killed by
    signal. Salvaged output keeps this on the degraded-verdict path rather than
    the no-model-output infrastructure abort (#1951).
    """
    return AgentResult(
        success=False,
        output="",
        session_id=None,
        cost_usd=0.21,
        exit_code=-9,
        raw={},
        profile_name="preflight",
        tool_trace=({"tool": "Read", "target": "README.md"},),
        partial_output="Waiting for the background investigation agent to finish.",
    )


# ── Tool surface: preflight cannot delegate ───────────────────────────────────


class TestPreflightToolSurface:
    def test_default_preflight_profile_excludes_bash(self):
        assert "Bash" not in DEFAULT_PREFLIGHT_PROFILE.allowed_tools
        assert DEFAULT_PREFLIGHT_PROFILE.allowed_tools == ("Read", "Glob", "Grep")

    def test_dev_and_review_tool_surfaces_are_unchanged(self):
        # Narrowing preflight must not narrow the roles that legitimately shell
        # out; they run to completion inside their turn.
        assert "Bash" in DEFAULT_DEV_PROFILE.allowed_tools
        assert "Bash" in DEFAULT_REVIEW_PROFILE.allowed_tools

    def test_plan_and_plan_review_defaults_keep_bash(self):
        # The plan / plan-review roles used to inherit their tool surface from
        # DEFAULT_PREFLIGHT_PROFILE. Narrowing preflight must not have narrowed
        # them by side effect.
        from theforge.config import DEFAULT_INVESTIGATION_TOOLS
        from theforge.config.schema import PlanRoleConfig, ReviewRoleConfig

        assert "Bash" in DEFAULT_INVESTIGATION_TOOLS
        assert "Bash" in PlanRoleConfig.allowed_tools
        assert "Bash" in ReviewRoleConfig.allowed_tools

    def test_prompt_forbids_background_delegation(self, tmp_path):
        task = _make_task(tmp_path)
        prompt = build_preflight_prompt(task, story_content="Some spec.")
        lowered = prompt.lower()
        assert "sub-agent" in lowered
        assert "background" in lowered
        assert "no delegation" in lowered

    def test_configured_bash_is_stripped_before_run_agent(self, tmp_path):
        """A forge.yaml override that re-grants Bash is sanitized, not honored.

        Both the primary and the fallback invocation pass through the same
        sanitizer, so a fallback profile cannot reintroduce the tool either.
        """
        config = _make_config(tmp_path)
        bash_preflight = ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash", "Glob", "Grep"),
            phase="preflight",
        )
        bash_fallback = ModelProfile(
            name="preflight_fallback",
            cli="claude",
            model="opus",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "bash", "Glob", "Grep"),
            phase="preflight",
        )
        config = config.__class__(
            **{
                **config.__dict__,
                "preflight_profile": bash_preflight,
                "preflight_fallback_profile": bash_fallback,
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch_gate_shell() as mock_shell,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            mock_preflight.side_effect = [
                _make_agent_result(success=False, output="", cost_usd=0.07),
                _make_agent_result(success=True, output=PREFLIGHT_PROCEED, cost_usd=0.13),
            ]
            mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
            mock_plan_agent.side_effect = mock_dev
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]

            run_task(config, task)

        assert mock_preflight.call_count == 2
        for call in mock_preflight.call_args_list:
            tools = [t.lower() for t in call.kwargs["profile"].allowed_tools]
            assert "bash" not in tools
            assert tools == ["read", "glob", "grep"]


# ── Coordinator seam: degraded state reaches routing and the phase record ─────


class TestDegradedPreflightSeam:
    def _run_with_degraded_preflight(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch_gate_shell() as mock_shell,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            mock_preflight.return_value = _crashed_with_salvaged_output()
            mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
            mock_plan_agent.side_effect = mock_dev
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            return run_task(config, task)

    def test_phase_end_fields_mark_the_complexity_unfounded(self, tmp_path):
        from theforge.coordinator.preflight_flow import _preflight_phase_end_fields

        result = self._run_with_degraded_preflight(tmp_path)
        fields = _preflight_phase_end_fields(result.state)

        assert fields["degraded"] is True
        assert fields["degraded_reason"] == "timeout_no_verdict"
        assert fields["failure_action"] == "proceed"
        # The score is still emitted — routing needs one — but it is labeled as
        # the conservative fallback rather than as an agent-founded figure.
        assert fields["complexity_score"] is not None
        assert fields["complexity_source"] == "preflight_degraded_conservative"

    def test_routing_audit_carries_the_degraded_source(self, tmp_path):
        result = self._run_with_degraded_preflight(tmp_path)
        audit = result.state.complexity_routing_audit

        assert isinstance(audit, dict)
        assert audit["complexity_source"] == "preflight_degraded_conservative"
        assert audit["preflight_degraded"]["degraded"] is True
        assert audit["preflight_degraded"]["degraded_reason"] == "timeout_no_verdict"

    def test_healthy_preflight_is_labeled_founded(self, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch_gate_shell() as mock_shell,
            patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
            patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
            patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
            patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        ):
            mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
            mock_preflight.return_value = _make_agent_result(
                success=True, output=PREFLIGHT_PROCEED, cost_usd=0.11
            )
            mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
            mock_plan_agent.side_effect = mock_dev
            mock_pool.return_value = [
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ]
            result = run_task(config, task)

        from theforge.coordinator.preflight_flow import _preflight_phase_end_fields

        fields = _preflight_phase_end_fields(result.state)
        assert fields["degraded"] is False
        assert fields["complexity_source"] == "preflight"


# ── Sprint record: the degradation reaches the summary row ───────────────────


class TestSummaryRowFields:
    def test_row_fields_from_state(self):
        from theforge.sprint.audit import preflight_degraded_row_fields

        class _State:
            preflight_degraded = True
            preflight_degraded_reason = "timeout_no_verdict"
            preflight_failure_action = "proceed"
            preflight_risk_signals = ["prior_execution_on_branch"]

        assert preflight_degraded_row_fields(_State()) == {
            "preflight_degraded": True,
            "preflight_degraded_reason": "timeout_no_verdict",
            "preflight_failure_action": "proceed",
            "preflight_risk_signals": ["prior_execution_on_branch"],
        }

    def test_row_fields_from_nested_audit_block(self):
        from theforge.sprint.audit import preflight_degraded_row_fields_from_audit

        assert preflight_degraded_row_fields_from_audit(
            {
                "degraded": True,
                "degraded_reason": "agent_failed_with_risk_signals",
                "failure_action": "escalate",
                "risk_signals": ["prior_execution_on_branch"],
            }
        ) == {
            "preflight_degraded": True,
            "preflight_degraded_reason": "agent_failed_with_risk_signals",
            "preflight_failure_action": "escalate",
            "preflight_risk_signals": ["prior_execution_on_branch"],
        }

    def test_healthy_run_records_the_absence_explicitly(self):
        from theforge.sprint.audit import preflight_degraded_row_fields

        class _State:
            preflight_degraded = False
            preflight_degraded_reason = None
            preflight_failure_action = None
            preflight_risk_signals: list[str] = []

        fields = preflight_degraded_row_fields(_State())
        assert fields["preflight_degraded"] is False
        assert fields["preflight_risk_signals"] == []


# ── Operator surfaces: status rows and the digest ─────────────────────────────


def _write_summary(tmp_path: Path, sprint_name: str, run_id: str, stories: list[dict]) -> Path:
    log_dir = tmp_path / ".forge" / "logs" / sprint_name
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / "sprint-summary.yaml"
    summary_path.write_text(
        yaml.safe_dump(
            {
                "sprint": {
                    "name": sprint_name,
                    "run_id": run_id,
                    "total_cost_usd": 4.20,
                    "duration_seconds": 600.0,
                },
                "stories": stories,
            }
        ),
        encoding="utf-8",
    )
    return summary_path


class TestStatusRowDisclosure:
    def test_done_row_still_reports_the_degradation(self, tmp_path):
        from theforge.sprint.status_reader import read_completed_status

        summary = _write_summary(
            tmp_path,
            "deg-sprint",
            "run-deg-1",
            [
                {
                    "path": "Issue #2346",
                    "slug": "issue-2346",
                    "outcome": "DONE",
                    "cost_usd": 4.20,
                    "preflight": "PROCEED",
                    "preflight_degraded": True,
                    "preflight_degraded_reason": "timeout_no_verdict",
                    "preflight_failure_action": "proceed",
                    "preflight_risk_signals": [],
                }
            ],
        )
        entries = read_completed_status(summary, tmp_path)

        assert len(entries) == 1
        assert "preflight degraded: timeout_no_verdict" in entries[0].detail
        assert "action=proceed" in entries[0].detail

    def test_older_summary_falls_back_to_the_nested_audit_block(self, tmp_path):
        from theforge.sprint.status_reader import read_completed_status

        summary = _write_summary(
            tmp_path,
            "deg-sprint",
            "run-deg-2",
            [
                {
                    "path": "Issue #2346",
                    "slug": "issue-2346",
                    "outcome": "DONE",
                    "cost_usd": 4.20,
                    "preflight": "PROCEED",
                }
            ],
        )
        audit_dir = summary.parent / "issue-2346"
        audit_dir.mkdir(parents=True, exist_ok=True)
        (audit_dir / "audit.yaml").write_text(
            yaml.safe_dump(
                {
                    "preflight": {
                        "verdict": "PROCEED",
                        "complexity": "large",
                        "degraded": True,
                        "degraded_reason": "parse_error",
                        "failure_action": "proceed",
                        "risk_signals": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        entries = read_completed_status(summary, tmp_path)

        assert "preflight degraded: parse_error" in entries[0].detail

    def test_healthy_row_gets_no_note(self, tmp_path):
        from theforge.sprint.status_reader import read_completed_status

        summary = _write_summary(
            tmp_path,
            "ok-sprint",
            "run-ok-1",
            [
                {
                    "path": "Issue #1",
                    "slug": "issue-1",
                    "outcome": "DONE",
                    "cost_usd": 1.0,
                    "preflight": "PROCEED",
                    "preflight_degraded": False,
                    "preflight_degraded_reason": None,
                    "preflight_failure_action": None,
                    "preflight_risk_signals": [],
                }
            ],
        )
        entries = read_completed_status(summary, tmp_path)

        assert "preflight degraded" not in entries[0].detail


class TestDigestDisclosure:
    def _render(self, tmp_path: Path, run_id: str) -> str:
        from theforge.cli.sprint_digest import display_sprint_digest

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = display_sprint_digest(run_id, tmp_path)
        assert rc == 0, buf.getvalue()
        return buf.getvalue()

    def test_all_done_sprint_still_renders_the_degradation(self, tmp_path):
        _write_summary(
            tmp_path,
            "deg-digest",
            "run-deg-3",
            [
                {
                    "path": "Issue #2346",
                    "slug": "issue-2346",
                    "outcome": "DONE",
                    "cost_usd": 4.20,
                    "merge": True,
                    "preflight_degraded": True,
                    "preflight_degraded_reason": "timeout_no_verdict",
                    "preflight_failure_action": "proceed",
                    "preflight_risk_signals": [],
                }
            ],
        )
        out = self._render(tmp_path, "run-deg-3")

        assert "PREFLIGHT DEGRADATIONS (1)" in out
        assert "reason: timeout_no_verdict" in out
        assert "action: proceed" in out
        assert "#2346" in out

    def test_escalated_story_appears_with_its_risk_signals(self, tmp_path):
        _write_summary(
            tmp_path,
            "esc-digest",
            "run-deg-4",
            [
                {
                    "path": "Issue #2346",
                    "slug": "issue-2346",
                    "outcome": "ESCALATE",
                    "cost_usd": 0.21,
                    "preflight_degraded": True,
                    "preflight_degraded_reason": "agent_failed_with_risk_signals",
                    "preflight_failure_action": "escalate",
                    "preflight_risk_signals": ["prior_execution_on_branch"],
                }
            ],
        )
        out = self._render(tmp_path, "run-deg-4")

        assert "PREFLIGHT DEGRADATIONS (1)" in out
        assert "action: escalate" in out
        assert "risk signals: prior_execution_on_branch" in out

    def test_healthy_sprint_renders_no_section(self, tmp_path):
        _write_summary(
            tmp_path,
            "ok-digest",
            "run-ok-2",
            [
                {
                    "path": "Issue #1",
                    "slug": "issue-1",
                    "outcome": "DONE",
                    "cost_usd": 1.0,
                    "merge": True,
                    "preflight_degraded": False,
                }
            ],
        )
        out = self._render(tmp_path, "run-ok-2")

        assert "PREFLIGHT DEGRADATIONS" not in out
