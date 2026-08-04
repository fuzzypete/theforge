"""Config-boundary coverage for score-driven reasoning effort (#1108).

``assignment.reasoning_effort`` is an integrity boundary: effort levels, score
bands, and token budgets are validated at load rather than silently dropped —
an override the operator wrote but that never applied is the failure mode this
guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.config import ProviderReasoningEffortConfig, ReasoningEffortConfig, load_config
from theforge.config.models import _parse_assignment, _parse_reasoning_effort


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


def test_defaults_are_empty_overrides():
    cfg = ReasoningEffortConfig()
    assert cfg.enabled is True
    assert cfg.phase_buckets == {}
    assert cfg.token_budgets == {}
    assert cfg.providers == {}
    # Defaults resolve through the routing SSOT, not a duplicated table here.
    assert cfg.token_budget_for(None, "low") == 2048
    assert cfg.token_budget_for(None, "medium") == 8192
    assert cfg.token_budget_for(None, "high") == 24576


def test_absent_block_yields_defaults():
    assert _parse_reasoning_effort(None) == ReasoningEffortConfig()
    assert _parse_assignment({}).reasoning_effort == ReasoningEffortConfig()


def test_parses_sprint_wide_and_per_provider_overrides():
    cfg = _parse_reasoning_effort(
        {
            "enabled": True,
            "phases": {
                "dev": [
                    {"max_score": 5, "effort": "low"},
                    {"max_score": 10, "effort": "high"},
                ]
            },
            "token_budgets": {"high": 32768},
            "providers": {
                "google": {
                    "phases": {"review": [{"max_score": 10, "effort": "medium"}]},
                    "token_budgets": {"medium": 4096},
                }
            },
        }
    )
    assert cfg.phase_buckets == {"dev": ((5, "low"), (10, "high"))}
    assert cfg.token_budgets == {"high": 32768}
    assert cfg.providers["google"].phase_buckets == {"review": ((10, "medium"),)}
    # Provider entries win per phase; unmentioned phases inherit sprint-wide.
    merged = cfg.buckets_for_provider("google")
    assert merged["dev"] == ((5, "low"), (10, "high"))
    assert merged["review"] == ((10, "medium"),)
    assert cfg.buckets_for_provider("codex") == {"dev": ((5, "low"), (10, "high"))}
    # Token budgets: provider → sprint-wide → routing default.
    assert cfg.token_budget_for("google", "medium") == 4096
    assert cfg.token_budget_for("google", "high") == 32768
    assert cfg.token_budget_for("google", "low") == 2048


def test_enabled_false_round_trips():
    assert _parse_reasoning_effort({"enabled": False}).enabled is False


@pytest.mark.parametrize(
    ("raw", "msg"),
    [
        ({"phases": {"dev": [{"max_score": 10, "effort": "maximum"}]}}, "effort must be one of"),
        ({"phases": {"dev": [{"max_score": 10, "effort": "LOW"}]}}, "effort must be one of"),
        ({"phases": {"dev": [{"max_score": 10, "effort": None}]}}, "effort must be one of"),
        ({"phases": {"deploy": [{"max_score": 10, "effort": "low"}]}}, "phase must be one of"),
        ({"phases": {"dev": []}}, "non-empty list"),
        ({"phases": {"dev": [{"max_score": 11, "effort": "low"}]}}, "ascend within 1-10"),
        ({"phases": {"dev": [{"max_score": 0, "effort": "low"}]}}, "ascend within 1-10"),
        (
            {
                "phases": {
                    "dev": [
                        {"max_score": 6, "effort": "low"},
                        {"max_score": 3, "effort": "high"},
                    ]
                }
            },
            "ascend within 1-10",
        ),
        ({"phases": {"dev": [{"max_score": 6, "effort": "low"}]}}, "cover scores through 10"),
        ({"phases": {"dev": [{"max_score": "6", "effort": "low"}]}}, "max_score must be an int"),
        ({"phases": {"dev": [{"max_score": True, "effort": "low"}]}}, "max_score must be an int"),
        ({"phases": {"dev": "high"}}, "non-empty list"),
        ({"phases": ["dev"]}, "must be a mapping"),
        ({"token_budgets": {"low": -1}}, "non-negative integer"),
        ({"token_budgets": {"low": "2048"}}, "non-negative integer"),
        ({"token_budgets": {"low": True}}, "non-negative integer"),
        ({"token_budgets": {"maximum": 2048}}, "effort must be one of"),
        ({"token_budgets": [1, 2]}, "must be a mapping"),
        ({"providers": ["google"]}, "providers must be a mapping"),
        ({"providers": {"google": "high"}}, "providers.google must be a mapping"),
        (
            {"providers": {"google": {"token_budgets": {"low": -5}}}},
            "providers.google.token_budgets",
        ),
    ],
)
def test_invalid_values_raise(raw, msg):
    with pytest.raises(ValueError, match=msg):
        _parse_reasoning_effort(raw)


def test_non_mapping_block_raises():
    with pytest.raises(ValueError, match="must be a mapping"):
        _parse_reasoning_effort(["enabled"])


def test_loads_from_real_forge_yaml(tmp_path):
    """The block must reach AssignmentConfig through the real loader, not just
    the parser helper — a config key nothing reads is the bug this catches."""
    config_path = _write_config(
        {
            "project": "test-project",
            "assignment": {
                "enabled": True,
                "reasoning_effort": {
                    "phases": {"dev": [{"max_score": 10, "effort": "high"}]},
                    "token_budgets": {"high": 32768},
                    "providers": {"google": {"token_budgets": {"high": 40000}}},
                },
            },
        },
        tmp_path,
    )
    config = load_config(config_path)
    cfg = config.assignment.reasoning_effort

    assert cfg.enabled is True
    assert cfg.phase_buckets == {"dev": ((10, "high"),)}
    assert cfg.providers["google"] == ProviderReasoningEffortConfig(token_budgets={"high": 40000})
    assert cfg.token_budget_for("google", "high") == 40000


def test_invalid_forge_yaml_fails_the_load(tmp_path):
    config_path = _write_config(
        {
            "project": "test-project",
            "assignment": {"reasoning_effort": {"token_budgets": {"extreme": 1}}},
        },
        tmp_path,
    )
    with pytest.raises(ValueError, match="effort must be one of"):
        load_config(config_path)
