"""Seam test: batch preflight cost flows into sprint total when stories are ALREADY_DONE.

When batch preflight returns ALREADY_DONE for all stories, the sprint summary must
reflect the preflight spend rather than reporting $0.00.

Issue: https://github.com/fuzzypete/theforge/issues/995
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_agent_result

from tests.test_sprint_resume import _make_config, _make_manifest, _make_spec_file
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

PREFLIGHT_COST_USD = 0.349


def _shell_side_effect(workspace_root: Path):
    def side_effect(cmd, cwd, **kwargs):
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
        if "git rev-parse" in rendered:
            return (True, "abc1234deadbeef")
        if "git log" in rendered and "--format=%ct" in rendered:
            return (True, "1713900000")
        if "git log" in rendered:
            return (True, "")
        if "--is-ancestor" in rendered:
            return (True, "")
        if "rev-list" in rendered and "--count" in rendered:
            return (True, "0")
        return (True, "")

    return side_effect


class TestSprintAlreadyDonePreflightCost:
    def test_sprint_total_includes_batch_preflight_cost_when_all_skipped(
        self, tmp_path: Path
    ) -> None:
        """Sprint total must include batch preflight cost when all stories are ALREADY_DONE.

        Seam boundary: batch preflight cost in CoordinatorState.preflight_result must
        survive apply_cached_preflight_state() and appear in result.state.total_cost,
        which the sprint accumulator then counts even for skipped stories.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        shell_side_effect = _shell_side_effect(tmp_path)

        def fake_preflight(*args, **kwargs):
            return _make_agent_result(
                success=True,
                output=ALREADY_DONE_OUTPUT,
                cost_usd=PREFLIGHT_COST_USD,
                profile_name="preflight",
            )

        with (
            patch("theforge.sprint.runner._run_baseline_gate", return_value={"passed": True}),
            patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=set()),
            patch("theforge.sprint.runner.sweep_orphan_worktrees"),
            patch("theforge.coordinator.util._run_shell", side_effect=shell_side_effect),
            patch("theforge.coordinator.preflight_flow.run_agent", side_effect=fake_preflight),
            patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=False),
        ):
            result = run_sprint(config, manifest_path, no_pull=True)

        assert result.specs_skipped == 1, (
            f"Expected 1 skipped story (ALREADY_DONE), got {result.specs_skipped}"
        )
        assert result.specs_succeeded == 0
        assert result.specs_failed == 0
        assert result.total_cost_usd == pytest.approx(PREFLIGHT_COST_USD, abs=1e-6), (
            f"Sprint total ${result.total_cost_usd:.4f} must include batch preflight "
            f"cost ${PREFLIGHT_COST_USD} — not $0.00"
        )
