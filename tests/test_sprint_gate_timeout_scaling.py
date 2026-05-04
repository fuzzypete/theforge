"""Adaptive gate-timeout scaling under sprint --parallel N.

Covers:
  - Pure resolver math (factor, mode, overcommit).
  - Seam-level integration: run_sprint resolves the effective timeout once at
    sprint start and propagates it through worker_config so the per-story
    coordinator invocation reads the scaled value via config.validation.gate_timeout.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

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
from theforge.sprint import run_sprint
from theforge.sprint.gate_timeout_resolver import resolve_effective_gate_timeout

# ── Pure resolver tests ───────────────────────────────────────────────────────


class TestResolveEffectiveGateTimeout:
    def test_parallel_one_factor_is_baseline(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=45, max_parallel=1, host_cores=10, gate_cpu_cores=7, mode="adaptive"
        )
        assert r.factor == 1.0
        assert r.effective_timeout == 45
        assert r.overcommit is False

    def test_parallel_n_scales_up(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=45, max_parallel=3, host_cores=10, gate_cpu_cores=7, mode="adaptive"
        )
        # demand = 21, factor = 2.1, effective = ceil(45*2.1) = 95
        assert r.factor == 2.1
        assert r.effective_timeout == 95
        assert r.overcommit is True

    def test_fixed_mode_preserves_baseline(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=45, max_parallel=3, host_cores=10, gate_cpu_cores=7, mode="fixed"
        )
        assert r.effective_timeout == 45
        assert r.mode == "fixed"

    def test_overcommit_threshold_is_1_5x_host(self) -> None:
        # demand = 12, host = 10, ratio = 1.2 → not overcommit
        r = resolve_effective_gate_timeout(
            baseline=60, max_parallel=2, host_cores=10, gate_cpu_cores=6, mode="adaptive"
        )
        assert r.overcommit is False

        # demand = 16, host = 10, ratio = 1.6 → overcommit
        r2 = resolve_effective_gate_timeout(
            baseline=60, max_parallel=2, host_cores=10, gate_cpu_cores=8, mode="adaptive"
        )
        assert r2.overcommit is True

    def test_gate_cpu_cores_none_defaults_to_host_cores(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=30, max_parallel=2, host_cores=8, gate_cpu_cores=None, mode="adaptive"
        )
        # demand = 8 * 2 = 16, factor = 2.0
        assert r.gate_cpu_cores == 8
        assert r.factor == 2.0
        assert r.effective_timeout == 60

    def test_unknown_mode_defaults_to_adaptive(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=10, max_parallel=2, host_cores=2, gate_cpu_cores=2, mode="bogus"
        )
        assert r.mode == "adaptive"
        assert r.effective_timeout == 20


# ── Seam-level integration: runner → coordinator config propagation ──────────


def _make_config(
    tmp_path: Path, *, gate_timeout: int, gate_cpu_cores: int | None, mode: str = "adaptive"
) -> ForgeConfig:
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
            gate_timeout_scale=mode,
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=1, max_review_cycles=1),
        sprint=SprintConfig(max_parallel=1),
    )


def _make_spec_file(tmp_path: Path, slug: str) -> None:
    (tmp_path / f"{slug}.md").write_text(
        f"---\nname: {slug}\nslug: {slug}\n---\n# {slug}\n",
        encoding="utf-8",
    )


def _make_manifest(tmp_path: Path, slugs: list[str], max_parallel: int) -> Path:
    path = tmp_path / "sprint.yaml"
    path.write_text(
        yaml.dump(
            {
                "name": "Scale Sprint",
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


def test_sprint_propagates_scaled_gate_timeout_to_run_task(tmp_path: Path) -> None:
    """worker_config seen by run_task must carry the scaled gate_timeout."""
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=7)

    captured: dict = {}

    def fake_run_task(cfg, task, **kwargs):  # type: ignore[no-untyped-def]
        captured["gate_timeout"] = cfg.validation.gate_timeout
        captured["mode"] = cfg.validation.gate_timeout_scale
        return _ok_result()

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("os.cpu_count", return_value=10),
    ):
        run_sprint(config, manifest_path)

    # Resolver math at parallel=3, gate_cpu=7, host=10 → factor=2.1 → 45*2.1 = 94.5 → ceil=95
    assert captured["gate_timeout"] == 95
    # The replace() must not corrupt other fields.
    assert captured["mode"] == "adaptive"


def test_sprint_fixed_mode_keeps_baseline(tmp_path: Path) -> None:
    """gate_timeout_scale=fixed pins the timeout regardless of --parallel."""
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=7, mode="fixed")

    captured: dict = {}

    def fake_run_task(cfg, task, **kwargs):  # type: ignore[no-untyped-def]
        captured["gate_timeout"] = cfg.validation.gate_timeout
        return _ok_result()

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("os.cpu_count", return_value=10),
    ):
        run_sprint(config, manifest_path)

    assert captured["gate_timeout"] == 45


def test_sprint_emits_overcommit_warning(tmp_path: Path, capsys) -> None:
    """When demand exceeds 1.5× host cores, an overcommit WARNING is logged."""
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=7)

    with (
        patch("theforge.sprint.runner.run_task", return_value=_ok_result()),
        patch("os.cpu_count", return_value=10),
    ):
        run_sprint(config, manifest_path)

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "overcommit" in err.lower() or "consider lowering --parallel" in err
    # And the resolution reasoning line:
    assert "gate_timeout: baseline=45s" in err
