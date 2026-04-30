"""Tests for stuck-detection threshold scaling by story complexity and plan size."""

from __future__ import annotations

from theforge.config import StuckDetectionConfig
from theforge.coordinator.dev_phase import _scale_stuck_for_complexity


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
