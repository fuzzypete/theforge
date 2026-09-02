"""Sprint pipeline smoke test for gate coverage.

Exercises the real sprint runner plus coordinator engine across the full mocked
pipeline:
WORKSPACE -> batch PREFLIGHT -> cached PREFLIGHT handoff -> PLAN ->
PLAN_REVIEW -> DEV -> VALIDATE -> REVIEW -> sprint landing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    APPROVE_REVIEW,
    _as_detailed,
    _make_agent_result,
    _write_handoff,
    patch_gate_shell,
)
from sprint_test_helpers import run_sprint_ctx

from tests.test_coord_plan_flow_agent import PLAN_AGENT_APPROVE
from tests.test_sprint_resume import _make_config
from theforge.config import PlanAgentReviewConfig, PlanConfig, RetryPolicy
from theforge.coordinator.state import CoordinatorState

PREFLIGHT_SMOKE_OUTPUT = """\
```yaml
verdict: PROCEED
complexity: large
complexity_score: 9
sufficiency: needs_planning
work_type: feature
bundle_candidate: false
likely_files:
  - src/theforge/coordinator/engine.py
  - src/theforge/sprint/runner.py
reason: "Synthetic smoke story is intentionally unfinished."
criteria_checked:
  - criterion: "Run every phase boundary once"
    satisfied: false
    evidence: "Smoke story fixture is synthetic"
```
"""


def _make_manifest(tmp_path: Path, story_file: str) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "name": "Pipeline Smoke Sprint",
                "budget_usd": 5.0,
                "stories": [story_file],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _make_story_file(tmp_path: Path) -> Path:
    story_path = tmp_path / "pipeline-smoke.md"
    story_path.write_text(
        "---\n"
        "name: Pipeline Smoke\n"
        "slug: pipeline-smoke\n"
        "---\n"
        "# Pipeline Smoke\n\n"
        "Synthetic story used only to verify sprint/coordinator phase seams.\n",
        encoding="utf-8",
    )
    return story_path


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"pipeline seam regression: {message}")


def _shell_for_smoke(workspace_root: Path):
    gate_calls = {"count": 0}

    def side_effect(cmd, cwd, **kwargs):  # noqa: ANN001
        rendered = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        cwd_path = Path(cwd)

        if rendered.startswith("mkdir -p "):
            target = rendered.removeprefix("mkdir -p ").strip()
            (workspace_root / target).mkdir(parents=True, exist_ok=True)
            return (True, "")

        if "git status --porcelain" in rendered:
            return (True, "")
        if "git worktree list --porcelain" in rendered:
            return (True, "")
        if "git branch --list" in rendered:
            return (True, "")
        if "git rev-parse --abbrev-ref HEAD" in rendered:
            return (True, f"forge/{cwd_path.name}")
        if "git log" in rendered and "--format=%ct" in rendered:
            return (True, "1713900000")
        if "git log" in rendered and "--oneline" in rendered:
            return (True, "abc1234 feat: pipeline smoke\n")
        if "git diff" in rendered and "--stat" in rendered:
            return (True, " src/theforge/coordinator/engine.py | 1 +\n")
        if "git diff" in rendered:
            return (True, "diff --git a/file b/file\n+synthetic change\n")
        if "git show " in rendered:
            return (True, "diff --git a/file b/file\n+synthetic change\n")
        if rendered.startswith("git add") or rendered.startswith("git commit"):
            return (True, "OK")
        if "make gate" in rendered or rendered == "gate":
            gate_calls["count"] += 1
            _write_handoff(cwd_path, "PASS")
            return (True, "OK")

        return (True, "OK")

    return side_effect, gate_calls


def test_make_gate_covers_mocked_sprint_pipeline_smoke_run(tmp_path: Path) -> None:
    story_path = _make_story_file(tmp_path)
    manifest_path = _make_manifest(tmp_path, story_path.name)
    config = replace(
        _make_config(tmp_path),
        plan=PlanConfig.of(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_agent_review=PlanAgentReviewConfig.of(enabled=True, min_reviewers=1),
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            max_dev_iterations_cap=4,
            max_review_cycles_cap=3,
            adaptive_iterations=True,
            preflight_complexity_gate_threshold=11,
        ),
    )

    shell_side_effect, gate_calls = _shell_for_smoke(tmp_path)
    phase_updates: list[dict] = []
    preflight_calls: list[Path] = []
    plan_calls: list[Path] = []
    dev_calls: list[Path] = []
    review_pool_calls: list[Path] = []
    plan_review_pool_calls: list[Path] = []
    landing_calls: list[tuple[str, Path]] = []
    cached_states_seen: list[CoordinatorState | None] = []

    def record_phase(update: dict) -> None:
        phase_updates.append(dict(update))

    def fake_preflight(*args, **kwargs):  # noqa: ANN001
        preflight_calls.append(Path(kwargs["working_dir"]))
        return _make_agent_result(
            success=True,
            output=PREFLIGHT_SMOKE_OUTPUT,
            cost_usd=0.05,
            profile_name="preflight",
        )

    def fake_plan(*args, **kwargs):  # noqa: ANN001
        plan_calls.append(Path(kwargs["working_dir"]))
        return _make_agent_result(
            success=True,
            output="# Plan\n\n1. Exercise the smoke pipeline.\n",
            cost_usd=0.10,
            profile_name="plan",
        )

    def fake_dev(*args, **kwargs):  # noqa: ANN001
        dev_calls.append(Path(kwargs["working_dir"]))
        return _make_agent_result(
            success=True,
            output="Implemented the synthetic smoke story.",
            cost_usd=0.20,
            profile_name="dev",
            dev_handoff={
                "summary": "Smoke pipeline executed end to end.",
                "commits": [{"sha": "abc1234", "message": "test(smoke): synthetic commit"}],
                "acceptance_criteria": [
                    {
                        "criterion": "All mocked pipeline phases ran",
                        "status": "MET",
                        "notes": "Synthetic story",
                    }
                ],
                "gate_result": "PASS",
                "story_deviations": "none",
                "deferred_items": "none",
            },
        )

    def fake_plan_review_pool(*args, **kwargs):  # noqa: ANN001
        plan_review_pool_calls.append(Path(kwargs["working_dir"]))
        return [
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_APPROVE,
                cost_usd=0.05,
                profile_name="plan-review",
            )
        ]

    def fake_review_pool(*args, **kwargs):  # noqa: ANN001
        review_pool_calls.append(Path(kwargs["working_dir"]))
        return [
            _make_agent_result(
                success=True,
                output=APPROVE_REVIEW,
                cost_usd=0.05,
                profile_name="review",
            )
        ]

    def fake_land_story(
        config, task, branch, worktree_path, parsed_review, state, effective_on_approve, **kwargs
    ):  # noqa: ANN001
        landing_calls.append((task.slug, Path(worktree_path)))
        return (
            {"merged": True, "action": effective_on_approve, "pending": False},
            "landed",
        )

    real_run_task = __import__("theforge.sprint.runner", fromlist=["run_task"]).run_task

    def record_cached_preflight(*args, **kwargs):  # noqa: ANN001
        cached_states_seen.append(kwargs.get("cached_preflight_state"))
        return real_run_task(*args, **kwargs)

    with (
        patch("theforge.sprint.runner._run_baseline_gate", return_value={"passed": True}),
        patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=set()),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch_gate_shell(
            side_effect=_as_detailed(shell_side_effect),
        ),
        patch("theforge.coordinator.preflight_flow.run_agent", side_effect=fake_preflight),
        patch("theforge.coordinator.plan_flow.run_agent", side_effect=fake_plan),
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=fake_dev),
        patch("theforge.coordinator.plan_flow.run_agent_pool", side_effect=fake_plan_review_pool),
        patch("theforge.coordinator.review_pool.run_agent_pool", side_effect=fake_review_pool),
        patch("theforge.coordinator.review_pool.log_agent_result"),
        patch("theforge.coordinator.completion.land_story", side_effect=fake_land_story),
        patch("theforge.sprint.runner.run_task", side_effect=record_cached_preflight),
    ):
        result = run_sprint_ctx(
            config,
            manifest_path,
            auto_merge=True,
            no_pull=True,
            state_update_fn=record_phase,
        )

    _require(
        result.specs_succeeded == 1,
        f"sprint landing did not complete successfully: {result}",
    )
    _require(gate_calls["count"] == 1, "VALIDATE gate did not run exactly once")
    _require(len(preflight_calls) == 1, "PREFLIGHT batch phase did not execute exactly once")
    _require(len(plan_calls) == 1, "PLAN phase did not invoke the planner once")
    _require(
        len(plan_review_pool_calls) == 1,
        "PLAN_REVIEW boundary did not invoke the plan review pool once",
    )
    _require(len(dev_calls) == 1, "DEV phase did not invoke the developer once")
    _require(len(review_pool_calls) == 1, "REVIEW phase did not invoke the review pool once")
    _require(len(landing_calls) == 1, "final landing did not execute exactly once")

    observed_worker_phases = [update["phase"] for update in phase_updates if update.get("phase")]
    for phase in ("WORKSPACE", "PLAN", "DEV", "VALIDATE", "REVIEW"):
        _require(
            phase in observed_worker_phases,
            (
                f"{phase} worker phase never surfaced through sprint state updates: "
                f"{observed_worker_phases}"
            ),
        )
    _require(
        observed_worker_phases.index("WORKSPACE") < observed_worker_phases.index("PLAN"),
        f"WORKSPACE -> PLAN boundary malformed: {observed_worker_phases}",
    )
    _require(
        observed_worker_phases.index("PLAN") < observed_worker_phases.index("DEV"),
        f"PLAN -> DEV boundary malformed: {observed_worker_phases}",
    )
    _require(
        observed_worker_phases.index("DEV") < observed_worker_phases.index("VALIDATE"),
        f"DEV -> VALIDATE boundary malformed: {observed_worker_phases}",
    )
    _require(
        observed_worker_phases.index("VALIDATE") < observed_worker_phases.index("REVIEW"),
        f"VALIDATE -> REVIEW boundary malformed: {observed_worker_phases}",
    )

    _require(
        len(cached_states_seen) == 1 and cached_states_seen[0] is not None,
        "post-PREFLIGHT engine run did not receive cached sprint preflight state",
    )

    final_story_result = result.results[0][1]
    state = final_story_result.state
    _require(state.preflight_cached, "engine did not mark the sprint PREFLIGHT handoff as cached")
    _require(
        state.preflight_cached_original_verdict == "PROCEED",
        (
            "cached PREFLIGHT verdict was lost at the sprint -> engine boundary: "
            f"{state.preflight_cached_original_verdict}"
        ),
    )
    _require(
        state.preflight_likely_files
        == [
            "src/theforge/coordinator/engine.py",
            "src/theforge/sprint/runner.py",
        ],
        f"cached PREFLIGHT likely_files were malformed: {state.preflight_likely_files}",
    )
    _require(
        state.preflight_sufficiency == "needs_planning",
        (
            "PREFLIGHT -> PLAN handoff lost sufficiency classification: "
            f"{state.preflight_sufficiency}"
        ),
    )
    _require(
        state.adaptive_dev_max > 0 and state.adaptive_review_max > 0,
        "adaptive limits were not populated before DEV/REVIEW execution",
    )
    _require(
        state.adaptive_limits_audit.get("complexity_score_used") == 9,
        f"adaptive limit audit lost the PREFLIGHT complexity score: {state.adaptive_limits_audit}",
    )
    _require(
        final_story_result.landing_status == "landed",
        (
            "sprint landing boundary did not report a landed story: "
            f"{final_story_result.landing_status}"
        ),
    )
