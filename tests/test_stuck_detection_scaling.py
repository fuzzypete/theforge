"""Tests for stuck-detection threshold scaling by story complexity and plan size."""

from __future__ import annotations

from unittest.mock import MagicMock

from theforge.config import StuckDetectionConfig
from theforge.coordinator.dev_phase import (
    _plan_files_for_stuck_scaling,
    _scale_stuck_for_complexity,
)
from theforge.coordinator.state import CoordinatorState


def test_scale_no_complexity_unchanged():
    cfg = StuckDetectionConfig()
    out = _scale_stuck_for_complexity(cfg, None, 0)
    assert out.no_progress_iterations == cfg.no_progress_iterations
    assert out.post_nudge_iterations == cfg.post_nudge_iterations


def test_scale_small_unchanged():
    cfg = StuckDetectionConfig()
    out = _scale_stuck_for_complexity(cfg, "small", 0)
    assert out.no_progress_iterations == cfg.no_progress_iterations
    assert out.post_nudge_iterations == cfg.post_nudge_iterations


def test_scale_medium_scales_up():
    cfg = StuckDetectionConfig(no_progress_iterations=5, post_nudge_iterations=3)
    out = _scale_stuck_for_complexity(cfg, "medium", 0)
    assert out.no_progress_iterations == round(5 * 1.5)
    assert out.post_nudge_iterations == round(3 * 1.5)


def test_scale_large_scales_up():
    cfg = StuckDetectionConfig(no_progress_iterations=5, post_nudge_iterations=3)
    out = _scale_stuck_for_complexity(cfg, "large", 0)
    assert out.no_progress_iterations == round(5 * 2.5)
    assert out.post_nudge_iterations == round(3 * 2.0)


def test_plan_file_count_added_to_no_progress():
    cfg = StuckDetectionConfig(no_progress_iterations=5, post_nudge_iterations=3)
    out = _scale_stuck_for_complexity(cfg, "large", 4)
    assert out.no_progress_iterations == round(5 * 2.5) + 4


def test_scale_never_lowers_thresholds():
    # If a complexity is unknown or multiplier < 1.0, scaling must not reduce.
    cfg = StuckDetectionConfig(
        no_progress_iterations=10,
        post_nudge_iterations=10,
        no_progress_multipliers={"tiny": 0.1},
        post_nudge_multipliers={"tiny": 0.1},
    )
    out = _scale_stuck_for_complexity(cfg, "tiny", 0)
    assert out.no_progress_iterations == 10
    assert out.post_nudge_iterations == 10


def test_user_override_multipliers_flow_through():
    cfg = StuckDetectionConfig(
        no_progress_iterations=4,
        post_nudge_iterations=2,
        no_progress_multipliers={"large": 5.0},
        post_nudge_multipliers={"large": 4.0},
    )
    out = _scale_stuck_for_complexity(cfg, "large", 0)
    assert out.no_progress_iterations == round(4 * 5.0)
    assert out.post_nudge_iterations == round(2 * 4.0)


def test_default_multiplier_dicts():
    cfg = StuckDetectionConfig()
    assert cfg.no_progress_multipliers == {"small": 1.0, "medium": 1.5, "large": 2.5}
    assert cfg.post_nudge_multipliers == {"small": 1.0, "medium": 1.5, "large": 2.0}


def test_unchanged_fields_preserved():
    cfg = StuckDetectionConfig(repeat_threshold=7, error_threshold=9, enabled=False)
    out = _scale_stuck_for_complexity(cfg, "large", 2)
    assert out.repeat_threshold == 7
    assert out.error_threshold == 9
    assert out.enabled is False


def test_plan_files_for_stuck_scaling_reads_declared_files():
    """A populated plan_structured yields its declared files — no warning emitted."""
    state = CoordinatorState()
    state.plan_structured = {
        "approach": "x",
        "steps": [
            {"id": 1, "files": ["src/a.py", "src/b.py"], "action": "modify"},
            {"id": 2, "files": ["src/b.py", "tests/test_a.py"], "action": "modify"},
        ],
    }
    logger = MagicMock()
    files = _plan_files_for_stuck_scaling(state, logger)
    assert files == ["src/a.py", "src/b.py", "tests/test_a.py"]
    logger._safe_emit.assert_not_called()


def test_plan_files_for_stuck_scaling_empty_plan_no_warning():
    """A well-formed plan that declares no files returns [] without warning.

    A file-less plan is distinct from a plan structure missing from state; only
    the latter is a policy-degrading anomaly worth surfacing.
    """
    state = CoordinatorState()
    state.plan_structured = {"approach": "x", "steps": []}
    logger = MagicMock()
    files = _plan_files_for_stuck_scaling(state, logger)
    assert files == []
    logger._safe_emit.assert_not_called()


def test_plan_files_for_stuck_scaling_warns_when_structure_missing():
    """None plan_structured surfaces a structured warning naming the field."""
    state = CoordinatorState()
    state.plan_structured = None
    state.dev_iteration = 3
    logger = MagicMock()
    files = _plan_files_for_stuck_scaling(state, logger)
    assert files == []
    logger._safe_emit.assert_called_once()
    event, kwargs = logger._safe_emit.call_args.args, logger._safe_emit.call_args.kwargs
    assert event[0] == "plan_structured_missing"
    assert kwargs["field"] == "plan_structured"
    assert kwargs["consumer"] == "stuck_detection_scaling"
    assert kwargs["iteration"] == 3


def test_plan_files_for_stuck_scaling_missing_structure_no_logger():
    """Missing structure with no logger still returns [] without raising."""
    state = CoordinatorState()
    state.plan_structured = None
    assert _plan_files_for_stuck_scaling(state, None) == []
