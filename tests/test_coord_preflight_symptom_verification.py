"""Tests for the bug ALREADY_DONE symptom-verification gate.

Refuting a hypothesized cause stated in an issue body — even when the
refutation is correct — must not by itself satisfy ``ALREADY_DONE`` for a
bug-typed story. The verdict requires an explicit ``symptom_verification``
block attesting that the originally observed symptom no longer reproduces
against the current baseline. Anything else (status missing, status not
``verified_resolved``, evidence absent or thin, ``reproduces_now: true``)
must demote the verdict to ``PROCEED`` so the dev cycle exercises the
symptom against the current codebase.

Non-bug work types are unaffected; AC-evidence checks via
``criteria_checked`` are sufficient there.
"""

from __future__ import annotations

from unittest.mock import patch

import yaml as _yaml
from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight import _parse_preflight_symptom_verification

# ── Fixtures ─────────────────────────────────────────────────────────────

# Bug ALREADY_DONE with full AC evidence AND verified_resolved symptom — honored.
PREFLIGHT_BUG_VERIFIED_RESOLVED = """\
```yaml
verdict: ALREADY_DONE
reason: "Diagnosis was wrong; symptom verified resolved."
complexity: small
sufficiency: implementation_ready
work_type: bug
criteria_checked:
  - criterion: "Codex CLI quota errors trigger provider_fallback"
    satisfied: true
    files_checked:
      - "src/theforge/config/load.py"
    runtime_path: "load_config -> _apply_transport_fallback at config/load.py:638"
    evidence: >-
      The configured transport_fallbacks.openai mapping is applied to every
      review-pool reviewer profile by load.py at the cited line, exercised
      by the live config loader path.
symptom_verification:
  status: verified_resolved
  reproduces_now: false
  evidence: >-
    Reproduced the quota path in a fixture: a Codex CLI reviewer hitting
    a _CLI_QUOTA_FALLBACK_PATTERNS error now switches to the API fallback
    profile rather than entering Pending Escalate Gate.
```
"""

# Bug ALREADY_DONE with AC evidence but no symptom_verification block — must downgrade.
# This is the exact failure shape the spec calls out: diagnosis section is
# refuted (criteria_checked says satisfied=true) but the symptom was never
# reproduced before closing the issue.
PREFLIGHT_BUG_NO_SYMPTOM_VERIFICATION = """\
```yaml
verdict: ALREADY_DONE
reason: "Diagnosis section claim was refuted by code inspection."
complexity: small
sufficiency: implementation_ready
work_type: bug
criteria_checked:
  - criterion: "Codex CLI quota errors trigger provider_fallback"
    satisfied: true
    files_checked:
      - "src/theforge/config/load.py"
    runtime_path: "load_config -> _apply_transport_fallback at config/load.py:638"
    evidence: >-
      The diagnosis section claimed no _apply_transport_fallback calls in any
      module that builds reviewer profiles. That claim is refuted by the
      call at config/load.py:638.
```
"""

# Bug ALREADY_DONE with status=not_attempted — must downgrade.
PREFLIGHT_BUG_NOT_ATTEMPTED = """\
```yaml
verdict: ALREADY_DONE
reason: "Diagnosis section claim was refuted; symptom not exercised."
complexity: small
sufficiency: implementation_ready
work_type: bug
criteria_checked:
  - criterion: "Codex CLI quota errors trigger provider_fallback"
    satisfied: true
    files_checked:
      - "src/theforge/config/load.py"
    runtime_path: "load_config -> _apply_transport_fallback at config/load.py:638"
    evidence: >-
      Refuted the diagnosis sentence by locating the call at the cited
      runtime path; the static evidence is concrete and present.
symptom_verification:
  status: not_attempted
  evidence: ""
```
"""

# Bug ALREADY_DONE with status=not_feasible — must downgrade.
PREFLIGHT_BUG_NOT_FEASIBLE = """\
```yaml
verdict: ALREADY_DONE
reason: "Cannot reproduce the symptom from preflight context."
complexity: small
sufficiency: implementation_ready
work_type: bug
criteria_checked:
  - criterion: "Codex CLI quota errors trigger provider_fallback"
    satisfied: true
    files_checked:
      - "src/theforge/config/load.py"
    runtime_path: "load_config -> _apply_transport_fallback at config/load.py:638"
    evidence: >-
      Static call site at config/load.py:638 wires the fallback into every
      review-pool profile through the live config loader path.
symptom_verification:
  status: not_feasible
  evidence: >-
    Reproducing the original quota failure requires a live Codex CLI
    session that exhausts quota; not reachable from preflight.
```
"""

# Bug ALREADY_DONE with reproduces_now=true — must downgrade even with status=verified_resolved
# (defensive: contradictory status+reproduces_now combination must not pass).
PREFLIGHT_BUG_STILL_REPRODUCES = """\
```yaml
verdict: ALREADY_DONE
reason: "Symptom still fires."
complexity: small
sufficiency: implementation_ready
work_type: bug
criteria_checked:
  - criterion: "Codex CLI quota errors trigger provider_fallback"
    satisfied: true
    files_checked:
      - "src/theforge/config/load.py"
    runtime_path: "load_config -> _apply_transport_fallback at config/load.py:638"
    evidence: >-
      Static call site at config/load.py:638 wires the fallback into every
      review-pool profile through the live config loader path.
symptom_verification:
  status: verified_resolved
  reproduces_now: true
  evidence: >-
    Exercising the quota path still routes to Pending Escalate Gate, the
    fallback does not fire as expected.
```
"""

# Bug ALREADY_DONE with thin evidence — must downgrade.
PREFLIGHT_BUG_THIN_EVIDENCE = """\
```yaml
verdict: ALREADY_DONE
reason: "Symptom resolved."
complexity: small
sufficiency: implementation_ready
work_type: bug
criteria_checked:
  - criterion: "Codex CLI quota errors trigger provider_fallback"
    satisfied: true
    files_checked:
      - "src/theforge/config/load.py"
    runtime_path: "load_config -> _apply_transport_fallback at config/load.py:638"
    evidence: >-
      Static call site at config/load.py:638 wires the fallback into every
      review-pool profile through the live config loader path.
symptom_verification:
  status: verified_resolved
  reproduces_now: false
  evidence: "fine"
```
"""

# Feature ALREADY_DONE with AC evidence and no symptom_verification — honored
# (the symptom-verification gate applies only to bug stories).
PREFLIGHT_FEATURE_NO_SYMPTOM_FIELD = """\
```yaml
verdict: ALREADY_DONE
reason: "Feature is observable in live code."
complexity: small
sufficiency: implementation_ready
work_type: feature
criteria_checked:
  - criterion: "Feature X is implemented"
    satisfied: true
    files_checked:
      - "src/theforge/coordinator/engine.py"
    runtime_path: "run_task -> _run_preflight_phase -> live engine path"
    evidence: >-
      The configured engine path executes the new behavior end-to-end as
      the acceptance criterion describes.
```
"""


def _make_bug_task(tmp_path):
    """Create a bug-typed task whose body has Diagnosis + Observed sections."""
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# Bug: provider_fallback not applied to review pool\n\n"
        "## Observed\n\n"
        "Codex CLI reviewer hit quota and the API fallback did not fire.\n\n"
        "## Diagnosis\n\n"
        "_apply_transport_fallback is missing from review pool config.\n",
        encoding="utf-8",
    )
    from theforge.task import TaskStory

    return TaskStory(
        name="Bug Task",
        story_path=spec,
        slug="test-bug-task",
        type="bug",
    )


# ── Parser tests ─────────────────────────────────────────────────────────


class TestParseSymptomVerification:
    def test_parses_verified_resolved(self) -> None:
        parsed = _parse_preflight_symptom_verification(PREFLIGHT_BUG_VERIFIED_RESOLVED)
        assert parsed["status"] == "verified_resolved"
        assert parsed["reproduces_now"] is False
        assert "Reproduced" in parsed["evidence"]

    def test_returns_empty_dict_when_field_absent(self) -> None:
        parsed = _parse_preflight_symptom_verification(PREFLIGHT_BUG_NO_SYMPTOM_VERIFICATION)
        assert parsed == {}

    def test_normalizes_unknown_status_to_empty(self) -> None:
        output = """```yaml
verdict: ALREADY_DONE
work_type: bug
symptom_verification:
  status: maybe_done
  evidence: "n/a"
```"""
        parsed = _parse_preflight_symptom_verification(output)
        assert parsed["status"] == ""

    def test_handles_non_bool_reproduces_now(self) -> None:
        output = """```yaml
verdict: ALREADY_DONE
work_type: bug
symptom_verification:
  status: verified_resolved
  reproduces_now: maybe
  evidence: "ok"
```"""
        parsed = _parse_preflight_symptom_verification(output)
        assert parsed["status"] == "verified_resolved"
        assert parsed["reproduces_now"] is None


# ── Integration tests through run_task ───────────────────────────────────


class TestBugAlreadyDoneSymptomGate:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_verified_resolved_keeps_already_done(
        self,
        mock_approve,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_bug_task(tmp_path)
        workspace = tmp_path / "test-bug-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BUG_VERIFIED_RESOLVED, cost_usd=0.05
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_verdict == "ALREADY_DONE"
        sv = result.state.preflight_symptom_verification
        assert sv["status"] == "verified_resolved"
        assert sv["reproduces_now"] is False
        mock_dev.assert_not_called()
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_missing_symptom_verification_downgrades_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """The headline regression: bug ALREADY_DONE with refuted diagnosis but
        no symptom verification must not close the issue."""
        config = _make_config(tmp_path)
        task = _make_bug_task(tmp_path)
        workspace = tmp_path / "test-bug-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BUG_NO_SYMPTOM_VERIFICATION, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        warnings = result.state.preflight_warnings or []
        assert any("symptom_verification absent" in w for w in warnings), warnings
        mock_dev.assert_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_not_attempted_status_downgrades_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_bug_task(tmp_path)
        workspace = tmp_path / "test-bug-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BUG_NOT_ATTEMPTED, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        warnings = result.state.preflight_warnings or []
        assert any("'not_attempted'" in w for w in warnings), warnings

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_not_feasible_status_downgrades_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_bug_task(tmp_path)
        workspace = tmp_path / "test-bug-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BUG_NOT_FEASIBLE, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        warnings = result.state.preflight_warnings or []
        assert any("'not_feasible'" in w for w in warnings), warnings

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_reproduces_now_true_downgrades_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_bug_task(tmp_path)
        workspace = tmp_path / "test-bug-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BUG_STILL_REPRODUCES, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        warnings = result.state.preflight_warnings or []
        assert any("reproduces_now=true" in w for w in warnings), warnings

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_thin_symptom_evidence_downgrades_to_proceed(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_bug_task(tmp_path)
        workspace = tmp_path / "test-bug-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BUG_THIN_EVIDENCE, cost_usd=0.05
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = mock_dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.preflight_verdict == "PROCEED"
        warnings = result.state.preflight_warnings or []
        assert any("evidence missing or too thin" in w for w in warnings), warnings

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_feature_already_done_unaffected_by_gate(
        self,
        mock_approve,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        """Feature stories without symptom_verification still honor ALREADY_DONE
        when AC evidence is present — the new gate is bug-specific."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)  # default feature-typed task
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_FEATURE_NO_SYMPTOM_FIELD, cost_usd=0.05
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.state.preflight_verdict == "ALREADY_DONE"
        mock_dev.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True)
    def test_symptom_verification_persists_in_artifact(
        self,
        mock_approve,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_bug_task(tmp_path)
        workspace = tmp_path / "test-bug-task"
        workspace.mkdir()
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BUG_VERIFIED_RESOLVED, cost_usd=0.05
        )

        result = run_task(config, task)

        assert result.success is True
        log_dir = result.state.log_dir
        assert log_dir is not None
        artifact = _yaml.safe_load((log_dir / "preflight.yaml").read_text(encoding="utf-8"))
        sv = artifact.get("symptom_verification")
        assert isinstance(sv, dict)
        assert sv["status"] == "verified_resolved"
        assert sv["reproduces_now"] is False
