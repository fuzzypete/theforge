"""Regression: intake remediation agent cost rolls up into sprint total.

Operator-visible accounting (sprint summary, audit YAML, forge status) must
include every dollar an agent spent inside a sprint launch. Intake
remediation auto-fix calls live outside CoordinatorState.total_cost; the
sprint runner is responsible for folding them into accumulated_cost.

Issue: https://github.com/fuzzypete/theforge/issues/1479
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_agent_result

from tests.test_sprint_resume import _make_config, _make_manifest, _make_spec_file
from theforge.config.types import IntakeConfig
from theforge.intake.remediation import IntakeOutcome, IntakeOutcomeKind
from theforge.sprint.runner import run_sprint

ALREADY_DONE_OUTPUT = """\
```yaml
verdict: ALREADY_DONE
complexity: small
complexity_score: 2
sufficiency: implementation_ready
work_type: feature
bundle_candidate: false
likely_files:
  - src/theforge/sprint/runner.py
reason: "All acceptance criteria are already satisfied."
criteria_checked:
  - criterion: "Feature is complete"
    satisfied: true
    files_checked:
      - src/theforge/sprint/runner.py
    runtime_path: src/theforge/sprint/runner.py
    evidence: "Function already exists and is complete."
```
"""

PREFLIGHT_COST_USD = 0.20
INTAKE_REMEDIATION_COST_USD = 0.10


def _shell_side_effect(workspace_root: Path):
    def side_effect(cmd, cwd, **kwargs):
        rendered = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        cwd_path = Path(cwd)
        if rendered.startswith("mkdir -p "):
            target = rendered.removeprefix("mkdir -p ").strip()
            (workspace_root / target).mkdir(parents=True, exist_ok=True)
            return (True, "")
        if "git rev-parse --abbrev-ref HEAD" in rendered:
            return (True, f"forge/{cwd_path.name}")
        if "git rev-parse" in rendered:
            return (True, "abc1234deadbeef")
        if "git log" in rendered and "--format=%ct" in rendered:
            return (True, "1713900000")
        if "rev-list" in rendered and "--count" in rendered:
            return (True, "0")
        return (True, "")

    return side_effect


def _config_with_intake(tmp_path: Path):
    base = _make_config(tmp_path)
    return dataclasses.replace(
        base,
        intake=IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="edit"),
    )


def _passed_outcome_with_cost(slug: str, cost: float) -> IntakeOutcome:
    return IntakeOutcome(
        slug=slug,
        kind=IntakeOutcomeKind.PASSED,
        findings=(),
        detail="",
        audit={
            "remediation_source": "agent",
            "mechanical_findings": [],
            "semantic_findings": [],
            "agent": {
                "attempted": True,
                "detail": "rewrote ACs",
                "profile_name": "intake",
                "model_used": "claude",
                "cost_usd": cost,
                "transport_used": "cli",
            },
            "issue_updated": True,
            "comment_posted": False,
        },
    )


class TestSprintIntakeRemediationCost:
    def test_sprint_total_includes_intake_remediation_agent_cost(self, tmp_path: Path) -> None:
        """A sprint that pays $0.10 for intake remediation must report it
        in result.total_cost_usd, not silently drop it.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _config_with_intake(tmp_path)

        shell_side_effect = _shell_side_effect(tmp_path)

        def fake_preflight(*args, **kwargs):
            return _make_agent_result(
                success=True,
                output=ALREADY_DONE_OUTPUT,
                cost_usd=PREFLIGHT_COST_USD,
                profile_name="preflight",
            )

        def fake_run_intake(_tasks, _root, **_kwargs):
            return {
                "feature-a": _passed_outcome_with_cost("feature-a", INTAKE_REMEDIATION_COST_USD)
            }

        with (
            patch(
                "theforge.sprint.runner._run_baseline_gate",
                return_value={"passed": True},
            ),
            patch(
                "theforge.sprint.runner.resolve_satisfied_dependencies",
                return_value=set(),
            ),
            patch("theforge.sprint.runner.sweep_orphan_worktrees"),
            patch(
                "theforge.sprint.runner.run_intake_remediation",
                side_effect=fake_run_intake,
            ),
            patch(
                "theforge.sprint.runner._build_intake_agent_caller",
                return_value=(lambda *a, **k: None, ""),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=shell_side_effect),
            patch(
                "theforge.coordinator.preflight_flow.run_agent",
                side_effect=fake_preflight,
            ),
            patch(
                "theforge.coordinator.preflight_flow._is_branch_merged",
                return_value=False,
            ),
        ):
            result = run_sprint(config, manifest_path, no_pull=True)

        expected_total = PREFLIGHT_COST_USD + INTAKE_REMEDIATION_COST_USD
        assert result.total_cost_usd == pytest.approx(expected_total, abs=1e-6), (
            f"Sprint total ${result.total_cost_usd:.4f} must include intake "
            f"remediation cost ${INTAKE_REMEDIATION_COST_USD} on top of "
            f"preflight cost ${PREFLIGHT_COST_USD} (expected ${expected_total})"
        )

        # Audit YAML and summary YAML must agree with SprintResult — both
        # write surfaces are operator-facing and must include intake spend.
        import yaml

        audit_yaml = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text()
        )
        assert audit_yaml["sprint"]["total_cost_usd"] == pytest.approx(expected_total, abs=1e-3), (
            "audit YAML total must include intake remediation cost"
        )

        summary_yaml = yaml.safe_load(
            (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
        )
        assert summary_yaml["sprint"]["total_cost_usd"] == pytest.approx(
            expected_total, abs=1e-3
        ), "summary YAML total must include intake remediation cost"

        # Persisted accumulated state must also reflect the intake-inclusive
        # per-story cost. Otherwise a later --resume reloads the stale total
        # and sprint-summary.yaml regresses to undercounting on the resume.
        sprint_id_path = tmp_path / ".forge" / "logs" / "Test Sprint" / ".sprint_id"
        sprint_id = sprint_id_path.read_text(encoding="utf-8").strip()
        state_yaml = yaml.safe_load(
            (tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml").read_text()
        )
        persisted = {row["slug"]: row for row in state_yaml.get("stories", [])}
        assert persisted["feature-a"]["cost_usd"] == pytest.approx(expected_total, abs=1e-3), (
            "persisted accumulated state must include intake remediation "
            "cost so resume reloads the correct total"
        )

    def test_entry_intake_outcomes_cost_rolls_into_sprint_total(self, tmp_path: Path) -> None:
        """Entry-level intake remediation (CLI pre-pass before run_sprint)
        passes outcomes via entry_intake_outcomes — those agent costs must
        also fold into the sprint total.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)  # intake disabled — only entry pass spent

        shell_side_effect = _shell_side_effect(tmp_path)

        def fake_preflight(*args, **kwargs):
            return _make_agent_result(
                success=True,
                output=ALREADY_DONE_OUTPUT,
                cost_usd=PREFLIGHT_COST_USD,
                profile_name="preflight",
            )

        entry_outcomes = {
            12345: _passed_outcome_with_cost("issue-12345", INTAKE_REMEDIATION_COST_USD)
        }

        with (
            patch(
                "theforge.sprint.runner._run_baseline_gate",
                return_value={"passed": True},
            ),
            patch(
                "theforge.sprint.runner.resolve_satisfied_dependencies",
                return_value=set(),
            ),
            patch("theforge.sprint.runner.sweep_orphan_worktrees"),
            patch("theforge.coordinator.util._run_shell", side_effect=shell_side_effect),
            patch(
                "theforge.coordinator.preflight_flow.run_agent",
                side_effect=fake_preflight,
            ),
            patch(
                "theforge.coordinator.preflight_flow._is_branch_merged",
                return_value=False,
            ),
        ):
            result = run_sprint(
                config,
                manifest_path,
                no_pull=True,
                entry_intake_outcomes=entry_outcomes,
            )

        expected_total = PREFLIGHT_COST_USD + INTAKE_REMEDIATION_COST_USD
        assert result.total_cost_usd == pytest.approx(expected_total, abs=1e-6), (
            f"Sprint total ${result.total_cost_usd:.4f} must include "
            f"entry-level intake cost ${INTAKE_REMEDIATION_COST_USD}"
        )


class TestResumeReloadsIntakeCost:
    """A two-run --resume flow must surface prior-run intake remediation
    cost in the resumed sprint-summary.yaml. Persistence of the accumulated
    per-story state has to write the intake-inclusive cost; otherwise the
    resumed summary loads the stale total and silently undercounts spend.
    """

    def test_resume_includes_prior_run_intake_remediation_cost(self, tmp_path: Path) -> None:
        import yaml

        from tests.test_sprint_run_id_rollover import (
            _make_coordinator_result,
        )
        from tests.test_sprint_run_id_rollover import (
            _make_manifest as _rollover_manifest,
        )
        from tests.test_sprint_run_id_rollover import (
            _make_spec_file as _spec,
        )
        from theforge.sprint.audit import (
            _get_or_create_sprint_id,
            _save_accumulated_stories,
        )
        from theforge.sprint.dag import StoryTriage
        from theforge.sprint.runner import run_sprint as _run_sprint

        _spec(tmp_path, "Feature A", "feature-a")
        _spec(tmp_path, "Feature B", "feature-b")
        manifest_path = _rollover_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        # Simulate a prior run that paid intake remediation cost on feature-a.
        # The prior run's accumulated state — written by _write_sprint_summary
        # AFTER canonical projection — must already include the intake cost.
        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        prior_dev_cost = 1.18
        prior_intake_cost = 0.10
        prior_total_per_story = prior_dev_cost + prior_intake_cost
        _save_accumulated_stories(
            sprint_id,
            sprint_name,
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "verdict": "APPROVE",
                    "cost_usd": prior_total_per_story,
                    "story_run_id": "run-a-test",
                    "preflight": "PROCEED",
                    "merge": True,
                    "depends_on": [],
                }
            ],
        )

        skip_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged",
            worktree_path=None,
            slug="feature-a",
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no prior",
            worktree_path=None,
            slug="feature-b",
        )

        def _mock_triage(canonical_ref, cfg, project_root, task=None, **_progress):
            slug = task.slug if task else Path(canonical_ref).stem
            return skip_triage if slug == "feature-a" else full_triage

        result_b = _make_coordinator_result(success=True, cost=9.26)

        with (
            patch("theforge.sprint.runner._triage_spec", side_effect=_mock_triage),
            patch("theforge.sprint.runner.run_task", return_value=result_b),
        ):
            sprint_result = _run_sprint(config, manifest_path, resume=True, run_id="run-b-test")

        # Resumed sprint-summary.yaml total includes feature-a's prior cost
        # (dev + intake) plus feature-b's current-run cost.
        summary = yaml.safe_load(
            (tmp_path / ".forge" / "logs" / sprint_name / "sprint-summary.yaml").read_text(
                encoding="utf-8"
            )
        )
        expected = prior_total_per_story + 9.26
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(expected), (
            "resumed summary YAML must include prior-run intake remediation "
            "cost via the persisted accumulated state"
        )
        # Per-story row also carries the intake-inclusive prior cost.
        by_slug = {s["slug"]: s for s in summary["stories"]}
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(prior_total_per_story)
        assert sprint_result.specs_succeeded == 2


class TestAllSkippedQueryModeIntakeCost:
    """Query-mode all-skipped path bypasses run_sprint and writes the audit
    via _emit_all_skipped_audit. Entry intake remediation may have spent
    agent budget before the all-skipped fork — the writer must include it.
    """

    def test_all_skipped_audit_includes_entry_intake_remediation_cost(
        self, tmp_path: Path
    ) -> None:
        from types import SimpleNamespace

        import yaml

        from theforge.cli.sprint import _emit_all_skipped_audit
        from theforge.sprint.shape_gate import SkippedIssue

        config = SimpleNamespace(project_root=tmp_path)
        skipped = [
            SkippedIssue(
                issue_number=4242,
                reason_codes=("missing_acceptance_criteria",),
                source="local_check",
                title="No ACs",
                detail="rejected at shape gate",
            ),
        ]
        intake_outcomes = {
            4242: _passed_outcome_with_cost("issue-4242", INTAKE_REMEDIATION_COST_USD),
        }

        _emit_all_skipped_audit(
            config=config,
            sprint_name="sprint-all-skipped-intake",
            budget_usd=10.0,
            skipped_issues=skipped,
            intake_outcomes=intake_outcomes,
        )

        summary_path = (
            tmp_path / ".forge" / "logs" / "sprint-all-skipped-intake" / "sprint-summary.yaml"
        )
        summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(
            INTAKE_REMEDIATION_COST_USD, abs=1e-6
        )

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        assert audit["sprint"]["total_cost_usd"] == pytest.approx(
            INTAKE_REMEDIATION_COST_USD, abs=1e-6
        )
