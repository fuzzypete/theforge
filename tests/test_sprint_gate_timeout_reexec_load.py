"""Gate-timeout derivation must model the load actually present.

Issue #2003: the effective gate timeout is derived from the sprint's *configured*
``--parallel``. On a mid-run re-exec the process additionally inherits the agent
groups still running from before, so the real concurrency is higher than the
model — and a gate that then exceeds its limit is reported as a broken merge
base, which is a conclusion about the code drawn from a measurement of our own
load.

Fresh-start derivation must be unchanged; continuation-time derivation must count
the inherited load and say so in the operator-facing diagnostic.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from sprint_test_helpers import run_sprint_ctx

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    SprintConfig,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.gate_timeout_resolver import resolve_effective_gate_timeout

# ── Pure resolver: running_stories is additive load ──────────────────


def test_running_stories_defaults_to_fresh_start_math() -> None:
    """Omitting running_stories reproduces the pre-existing derivation exactly."""
    fresh = resolve_effective_gate_timeout(
        baseline=60, max_parallel=2, host_cores=10, gate_cpu_cores=10, mode="adaptive"
    )
    explicit_zero = resolve_effective_gate_timeout(
        baseline=60,
        max_parallel=2,
        host_cores=10,
        gate_cpu_cores=10,
        mode="adaptive",
        running_stories=0,
    )
    # The incident's own startup line: parallel=2, gate_cpu=10, host=10 → 120s.
    assert fresh.effective_timeout == 120
    assert fresh.factor == 2.0
    assert fresh.running_stories == 0
    assert fresh.actual_parallel == 2
    assert explicit_zero == fresh


def test_running_stories_raises_demand_and_timeout() -> None:
    """Two inherited agents alongside parallel=2 is a load of four, not two."""
    r = resolve_effective_gate_timeout(
        baseline=60,
        max_parallel=2,
        host_cores=10,
        gate_cpu_cores=10,
        mode="adaptive",
        running_stories=2,
    )
    assert r.actual_parallel == 4
    assert r.factor == 4.0
    assert r.effective_timeout == 240
    assert r.overcommit is False
    # Configured parallelism is still reported as itself — the two numbers are
    # different facts and the diagnostic keeps both.
    assert r.max_parallel == 2
    assert "running_stories=2 actual_parallel=4" in r.reason


def test_fixed_mode_ignores_inherited_load() -> None:
    """``fixed`` means fixed: an operator pin is not overridden by contention."""
    r = resolve_effective_gate_timeout(
        baseline=60,
        max_parallel=2,
        host_cores=10,
        gate_cpu_cores=10,
        mode="fixed",
        running_stories=3,
    )
    assert r.effective_timeout == 60
    assert r.running_stories == 3


def test_running_stories_warns_only_with_high_observed_load() -> None:
    r = resolve_effective_gate_timeout(
        baseline=60,
        max_parallel=2,
        host_cores=10,
        gate_cpu_cores=10,
        mode="adaptive",
        running_stories=2,
        observed_host_load=12.2,
    )
    assert r.overcommit is True
    assert r.warning_host_load == 10.2
    assert "observed_host_load=12.20" in r.reason
    assert "warning_host_load=10.20" in r.reason


# ── Seam: runner supplies the actual load ────────────────────────────


def _make_config(tmp_path: Path, *, gate_timeout: int, gate_cpu_cores: int) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=replace(
            DEFAULT_VALIDATION,
            gate_timeout=gate_timeout,
            gate_cpu_cores=gate_cpu_cores,
            gate_timeout_scale="adaptive",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=1, max_review_cycles=1),
        sprint=SprintConfig(max_parallel=1),
    )


def _make_manifest(tmp_path: Path, slugs: list[str], max_parallel: int) -> Path:
    for slug in slugs:
        (tmp_path / f"{slug}.md").write_text(
            f"---\nname: {slug}\nslug: {slug}\n---\n# {slug}\n", encoding="utf-8"
        )
    path = tmp_path / "sprint.yaml"
    path.write_text(
        yaml.dump(
            {
                "name": "Load Sprint",
                "budget_usd": 10.0,
                "stories": [f"{s}.md" for s in slugs],
                "max_parallel": max_parallel,
            }
        ),
        encoding="utf-8",
    )
    return path


def _ok_result() -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    pf = MagicMock()
    pf.cost_usd = 0.0
    state.preflight_result = pf
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")


def test_fresh_start_timeout_derives_from_configured_parallel(tmp_path: Path, capsys) -> None:
    """No inherited load on a fresh start: the diagnostic says so and the
    effective timeout is the configured-parallelism one."""
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=2)
    config = _make_config(tmp_path, gate_timeout=60, gate_cpu_cores=10)

    captured: dict = {}

    def fake_run_task(cfg, task, **kwargs):  # type: ignore[no-untyped-def]
        captured["gate_timeout"] = cfg.validation.gate_timeout
        return _ok_result()

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("os.cpu_count", return_value=10),
        patch("os.getloadavg", return_value=(6.68, 7.0, 5.52)),
    ):
        run_sprint_ctx(config, manifest_path)

    err = capsys.readouterr().err
    assert "running_stories=0 actual_parallel=2 observed_host_load=6.68" in err
    assert captured["gate_timeout"] == 120
    assert "WARNING: gate CPU" not in err


def test_continuation_timeout_counts_inherited_running_stories(tmp_path: Path, capsys) -> None:
    """On a re-exec, the surviving agent counts toward the derived limit."""
    manifest_path = _make_manifest(tmp_path, ["story-a", "story-b"], max_parallel=2)
    config = _make_config(tmp_path, gate_timeout=60, gate_cpu_cores=10)

    captured: dict = {}

    def fake_run_task(cfg, task, **kwargs):  # type: ignore[no-untyped-def]
        captured["gate_timeout"] = cfg.validation.gate_timeout
        return _ok_result()

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("os.cpu_count", return_value=10),
        patch("os.getloadavg", return_value=(10.2, 10.0, 9.8)),
    ):
        run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            live_story_slugs={"story-a"},
        )

    err = capsys.readouterr().err
    assert (
        "running_stories=1 actual_parallel=3 observed_host_load=10.20 "
        "warning_host_load=9.20" in err
    )
    assert "WARNING: gate CPU" not in err
    # 60s baseline × (10 cores × 3) / 10 host cores = 180s, not the 120s a model
    # blind to the inherited agent would have produced.
    assert captured["gate_timeout"] == 180


def test_continuation_warning_requires_load_beyond_inherited_stories(
    tmp_path: Path, capsys
) -> None:
    manifest_path = _make_manifest(tmp_path, ["story-a", "story-b"], max_parallel=2)
    config = _make_config(tmp_path, gate_timeout=60, gate_cpu_cores=10)

    with (
        patch("theforge.sprint.runner.run_task", return_value=_ok_result()),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("os.cpu_count", return_value=10),
        patch("os.getloadavg", return_value=(11.2, 10.9, 10.6)),
    ):
        run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            live_story_slugs={"story-a"},
        )

    err = capsys.readouterr().err
    assert "WARNING: gate CPU observed host load (11.20 1m / 10 cores)" in err
    assert "after discounting 1 inherited story (10.20 effective)" in err
