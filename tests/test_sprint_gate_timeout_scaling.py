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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
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
        assert r.overcommit is False

    def test_fixed_mode_preserves_baseline(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=45, max_parallel=3, host_cores=10, gate_cpu_cores=7, mode="fixed"
        )
        assert r.effective_timeout == 45
        assert r.mode == "fixed"

    def test_observed_host_load_controls_overcommit(self) -> None:
        # demand still scales the timeout, but warning eligibility follows observed host load.
        r = resolve_effective_gate_timeout(
            baseline=60,
            max_parallel=2,
            host_cores=10,
            gate_cpu_cores=6,
            mode="adaptive",
            observed_host_load=6.68,
        )
        assert r.overcommit is False
        assert r.warning_host_load == 6.68

        r2 = resolve_effective_gate_timeout(
            baseline=60,
            max_parallel=2,
            host_cores=10,
            gate_cpu_cores=8,
            mode="adaptive",
            observed_host_load=10.1,
        )
        assert r2.overcommit is True
        assert r2.warning_host_load == 10.1

    def test_gate_cpu_cores_none_defaults_to_host_cores_without_warning_on_low_load(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=30,
            max_parallel=2,
            host_cores=10,
            gate_cpu_cores=None,
            mode="adaptive",
            observed_host_load=6.68,
        )
        # demand = 10 * 2 = 20, factor = 2.0
        assert r.gate_cpu_cores == 10
        assert r.factor == 2.0
        assert r.effective_timeout == 60
        assert r.overcommit is False

    def test_load_unavailable_suppresses_overcommit_warning(self) -> None:
        r = resolve_effective_gate_timeout(
            baseline=30, max_parallel=4, host_cores=10, gate_cpu_cores=10, mode="adaptive"
        )
        assert r.overcommit is False
        assert r.observed_host_load is None

    def test_unknown_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="gate_timeout_scale must be 'adaptive' or 'fixed'"):
            resolve_effective_gate_timeout(
                baseline=10, max_parallel=2, host_cores=2, gate_cpu_cores=2, mode="bogus"
            )


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
        run_sprint_ctx(config, manifest_path)

    # Resolver math at parallel=3, gate_cpu=7, host=10 → factor=2.1 → 45*2.1 = 94.5 → ceil=95
    assert captured["gate_timeout"] == 95
    # The replace() must not corrupt other fields.
    assert captured["mode"] == "adaptive"


def test_sprint_propagates_scaled_workspace_setup_timeout_to_run_task(
    tmp_path: Path, capsys
) -> None:
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=7)
    config = replace(
        config,
        workspace=replace(config.workspace, setup_command="pip install -e ."),
    )

    captured: dict = {}

    def fake_run_task(cfg, task, **kwargs):  # type: ignore[no-untyped-def]
        captured["setup_timeout"] = cfg.workspace.setup_timeout
        return _ok_result()

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("os.cpu_count", return_value=10),
    ):
        run_sprint_ctx(config, manifest_path)

    err = capsys.readouterr().err
    assert captured["setup_timeout"] == 252
    assert "workspace.setup_timeout: baseline=120s mode=adaptive parallel=3" in err


def test_sprint_skips_workspace_setup_timeout_rebind_for_non_dataclass_workspace(
    tmp_path: Path, capsys
) -> None:
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=7)
    workspace_data = vars(config.workspace).copy()
    workspace_data["setup_command"] = "pip install -e ."
    config = replace(config, workspace=SimpleNamespace(**workspace_data))

    captured: dict = {}

    def fake_run_task(cfg, task, **kwargs):  # type: ignore[no-untyped-def]
        captured["gate_timeout"] = cfg.validation.gate_timeout
        captured["setup_timeout"] = cfg.workspace.setup_timeout
        return _ok_result()

    with (
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("os.cpu_count", return_value=10),
    ):
        run_sprint_ctx(config, manifest_path)

    err = capsys.readouterr().err
    assert captured["gate_timeout"] == 95
    assert captured["setup_timeout"] == 120
    assert "workspace.setup_timeout: baseline=120s mode=adaptive parallel=3" in err
    assert "candidate_effective=252s effective=120s" in err
    assert "workspace_rebind=skipped(non-dataclass)" in err


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
        run_sprint_ctx(config, manifest_path)

    assert captured["gate_timeout"] == 45


def test_sprint_emits_overcommit_warning(tmp_path: Path, capsys) -> None:
    """Observed host contention, not synthetic demand alone, triggers the warning."""
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=7)

    with (
        patch("theforge.sprint.runner.run_task", return_value=_ok_result()),
        patch("os.cpu_count", return_value=10),
        patch("os.getloadavg", return_value=(10.1, 9.8, 9.4)),
    ):
        run_sprint_ctx(config, manifest_path)

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "observed host load (10.10 1m / 10 cores)" in err
    assert "expanded gate_timeout may still be insufficient" in err
    assert "gate_timeout: baseline=45s" in err


def test_sprint_fixed_mode_warning_mentions_unchanged_baseline(tmp_path: Path, capsys) -> None:
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=7, mode="fixed")

    with (
        patch("theforge.sprint.runner.run_task", return_value=_ok_result()),
        patch("os.cpu_count", return_value=10),
        patch("os.getloadavg", return_value=(10.1, 9.8, 9.4)),
    ):
        run_sprint_ctx(config, manifest_path)

    err = capsys.readouterr().err
    assert "WARNING: gate CPU observed host load (10.10 1m / 10 cores)" in err
    assert "fixed gate_timeout leaves the baseline unchanged" in err


def test_sprint_skips_overcommit_warning_when_load_is_low(tmp_path: Path, capsys) -> None:
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=2)
    config = _make_config(tmp_path, gate_timeout=45, gate_cpu_cores=None)

    with (
        patch("theforge.sprint.runner.run_task", return_value=_ok_result()),
        patch("os.cpu_count", return_value=10),
        patch("os.getloadavg", return_value=(6.68, 7.0, 5.52)),
    ):
        run_sprint_ctx(config, manifest_path)

    err = capsys.readouterr().err
    assert "gate_timeout: baseline=45s mode=adaptive parallel=2 gate_cpu_cores=10" in err
    assert "WARNING: gate CPU" not in err


def test_sprint_invalid_gate_timeout_scale_fails_fast(tmp_path: Path) -> None:
    """A malformed gate_timeout_scale must raise, not silently disable scaling.

    theforge.config.load rejects this at config-load time, but run_sprint
    itself must also fail fast rather than swallow the error — the coordinator
    control-flow boundary must not be the only guard.
    """
    _make_spec_file(tmp_path, "story-a")
    manifest_path = _make_manifest(tmp_path, ["story-a"], max_parallel=3)
    config = _make_config(
        tmp_path, gate_timeout=45, gate_cpu_cores=7, mode="gate_timeout_scale: typo"
    )

    with (
        patch("theforge.sprint.runner.run_task", return_value=_ok_result()),
        patch("os.cpu_count", return_value=10),
    ):
        with pytest.raises(ValueError, match="gate_timeout_scale must be 'adaptive' or 'fixed'"):
            run_sprint_ctx(config, manifest_path)
