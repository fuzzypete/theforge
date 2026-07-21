"""Config-boundary coverage for exploration settings (#325).

The ``assignment.exploration`` block is an integrity boundary: cadence, sample
floor, and per-sprint cap are bounds-checked at load, and the performance-cache
path is a location, not an authority.
"""

from __future__ import annotations

import pytest

from theforge.config import ExplorationConfig
from theforge.config.models import _parse_exploration


def test_defaults():
    cfg = ExplorationConfig()
    assert cfg.explore_every_n == 5
    assert cfg.min_sample_size == 3
    assert cfg.per_sprint_cap == 1
    assert cfg.performance_cache_path == ".forge/performance_table.yaml"


def test_parse_none_returns_defaults():
    assert _parse_exploration(None) == ExplorationConfig()


def test_parse_valid_overrides():
    cfg = _parse_exploration(
        {
            "explore_every_n": 10,
            "min_sample_size": 5,
            "per_sprint_cap": 2,
            "performance_cache_path": ".forge/custom.yaml",
        }
    )
    assert cfg.explore_every_n == 10
    assert cfg.min_sample_size == 5
    assert cfg.per_sprint_cap == 2
    assert cfg.performance_cache_path == ".forge/custom.yaml"


def test_per_sprint_cap_zero_is_allowed_and_disables():
    assert _parse_exploration({"per_sprint_cap": 0}).per_sprint_cap == 0


@pytest.mark.parametrize(
    "raw,msg",
    [
        ({"explore_every_n": 0}, "explore_every_n"),
        ({"explore_every_n": -1}, "explore_every_n"),
        ({"min_sample_size": 0}, "min_sample_size"),
        ({"per_sprint_cap": -1}, "per_sprint_cap"),
        ({"explore_every_n": "five"}, "explore_every_n"),
        ({"explore_every_n": True}, "explore_every_n"),  # bool rejected
        ({"performance_cache_path": ""}, "performance_cache_path"),
        ({"performance_cache_path": 5}, "performance_cache_path"),
    ],
)
def test_invalid_values_raise(raw, msg):
    with pytest.raises(ValueError, match=msg):
        _parse_exploration(raw)


def test_non_mapping_raises():
    with pytest.raises(ValueError, match="must be a mapping"):
        _parse_exploration([1, 2, 3])


def test_assignment_config_carries_exploration():
    from theforge.config.models import _parse_assignment

    cfg = _parse_assignment({"exploration": {"explore_every_n": 7}})
    assert cfg.exploration.explore_every_n == 7
    # Absent block → defaults.
    assert _parse_assignment({}).exploration == ExplorationConfig()
