"""Tests for ``forge profiles reset`` and related profile-maintenance helpers."""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.cli.main import build_parser
from theforge.cli.profiles import cmd_profiles
from theforge.model_profiles import (
    get_dev_success_rate,
    load_profiles,
    reset_profile_data,
    save_profiles,
)

CANONICAL_ID = "anthropic/sonnet/cli"


def _profiles_fixture() -> dict:
    return {
        "models": {
            CANONICAL_ID: {
                "_identity": {
                    "provider": "anthropic",
                    "model": "sonnet",
                    "transport": "cli",
                    "cli": "claude",
                },
                "dev": {
                    "runs": 12,
                    "_successes": 7,
                    "_iterations_sum": 16.0,
                    "_cost_sum": 1.2,
                    "success_rate": round(7 / 12, 4),
                    "avg_iterations": round(16.0 / 12, 4),
                    "avg_cost_usd": round(1.2 / 12, 6),
                    "by_complexity": {
                        "small": {
                            "runs": 5,
                            "_successes": 2,
                            "_iterations_sum": 5.0,
                            "_cost_sum": 0.5,
                            "success_rate": 0.4,
                            "avg_iterations": 1.0,
                            "avg_cost_usd": 0.1,
                        },
                        "medium": {
                            "runs": 4,
                            "_successes": 3,
                            "_iterations_sum": 5.0,
                            "_cost_sum": 0.4,
                            "success_rate": 0.75,
                            "avg_iterations": 1.25,
                            "avg_cost_usd": 0.1,
                        },
                        "large": {
                            "runs": 3,
                            "_successes": 2,
                            "_iterations_sum": 6.0,
                            "_cost_sum": 0.3,
                            "success_rate": round(2 / 3, 4),
                            "avg_iterations": 2.0,
                            "avg_cost_usd": 0.1,
                        },
                    },
                },
                "review": {
                    "runs": 6,
                    "_findings_sum": 15.0,
                    "_cost_sum": 0.9,
                    "avg_findings": 2.5,
                    "avg_cost_usd": 0.15,
                },
                "preflight": {
                    "runs": 3,
                    "_cost_sum": 0.21,
                    "avg_cost_usd": 0.07,
                },
            }
        }
    }


def _write_profiles(tmp_path: Path, data: dict | None = None) -> Path:
    project_root = tmp_path
    profiles_path = project_root / ".forge" / "model_profiles.yaml"
    save_profiles(profiles_path, data or _profiles_fixture())
    return project_root


def _read_reset_history(project_root: Path) -> dict:
    history_path = project_root / ".forge" / "profiles" / "reset-history.yaml"
    return yaml.safe_load(history_path.read_text(encoding="utf-8"))


def test_reset_profile_data_full_reset_clears_model_history():
    updated, _pre_reset = reset_profile_data(_profiles_fixture(), CANONICAL_ID)

    dev = updated["models"][CANONICAL_ID]["dev"]
    assert dev["runs"] == 0
    assert dev["_successes"] == 0
    assert all(dev["by_complexity"][band]["runs"] == 0 for band in ("small", "medium", "large"))
    assert updated["models"][CANONICAL_ID]["review"]["runs"] == 0
    assert updated["models"][CANONICAL_ID]["preflight"]["runs"] == 0
    assert get_dev_success_rate(updated, CANONICAL_ID, "small") is None


def test_reset_profile_data_role_scoped_reset_preserves_other_roles():
    updated, pre_reset = reset_profile_data(_profiles_fixture(), CANONICAL_ID, role="review")

    assert pre_reset == [
        {
            "role": "review",
            "complexity": None,
            "runs": 6,
            "avg_cost_usd": 0.15,
            "cost_unknown_runs": 0,
            "avg_findings": 2.5,
        }
    ]
    assert updated["models"][CANONICAL_ID]["review"]["runs"] == 0
    assert updated["models"][CANONICAL_ID]["dev"]["runs"] == 12
    assert updated["models"][CANONICAL_ID]["preflight"]["runs"] == 3


def test_reset_profile_data_complexity_scoped_reset_preserves_other_buckets():
    updated, pre_reset = reset_profile_data(_profiles_fixture(), CANONICAL_ID, complexity="medium")

    assert pre_reset == [
        {
            "role": "dev",
            "complexity": "medium",
            "runs": 4,
            "successes": 3,
            "avg_iterations": 1.25,
            "avg_cost_usd": 0.1,
            "cost_unknown_runs": 0,
        }
    ]
    dev = updated["models"][CANONICAL_ID]["dev"]
    assert dev["by_complexity"]["medium"]["runs"] == 0
    assert dev["by_complexity"]["small"]["runs"] == 5
    assert dev["by_complexity"]["large"]["runs"] == 3
    assert dev["runs"] == 8
    assert dev["_successes"] == 4


def test_reset_profile_data_combined_scope_resets_one_role_bucket():
    updated, _pre_reset = reset_profile_data(
        _profiles_fixture(),
        CANONICAL_ID,
        role="dev",
        complexity="small",
    )

    dev = updated["models"][CANONICAL_ID]["dev"]
    assert dev["by_complexity"]["small"]["runs"] == 0
    assert dev["by_complexity"]["medium"]["runs"] == 4
    assert dev["by_complexity"]["large"]["runs"] == 3
    assert dev["runs"] == 7
    assert dev["_successes"] == 5


def test_profiles_reset_command_audits_pre_reset_counts_and_prints_reason(capsys, tmp_path):
    project_root = _write_profiles(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "profiles",
            "reset",
            "--project-root",
            str(project_root),
            "--model",
            CANONICAL_ID,
            "--role",
            "dev",
            "--complexity",
            "small",
            "--reason",
            "stuck detection false positive",
        ]
    )

    assert cmd_profiles(args) == 0

    stdout = capsys.readouterr().out
    assert "Reset anthropic/sonnet/cli dev small history." in stdout
    assert "Reason given: stuck detection false positive" in stdout

    reloaded = load_profiles(project_root / ".forge" / "model_profiles.yaml")
    assert reloaded["models"][CANONICAL_ID]["dev"]["by_complexity"]["small"]["runs"] == 0

    history = _read_reset_history(project_root)
    [entry] = history["resets"]
    assert entry["scope"] == {
        "canonical_id": CANONICAL_ID,
        "role": "dev",
        "complexity": "small",
    }
    assert entry["reason"] == "stuck detection false positive"
    assert entry["pre_reset"] == [
        {
            "role": "dev",
            "complexity": "small",
            "runs": 5,
            "successes": 2,
            "avg_iterations": 1.0,
            "avg_cost_usd": 0.1,
            "cost_unknown_runs": 0,
        }
    ]


def test_profiles_reset_command_on_missing_history_is_noop_not_error(tmp_path):
    project_root = tmp_path
    parser = build_parser()
    args = parser.parse_args(
        [
            "profiles",
            "reset",
            "--project-root",
            str(project_root),
            "--model",
            "openai/gpt-5.4/cli",
        ]
    )

    assert cmd_profiles(args) == 0

    history = _read_reset_history(project_root)
    [entry] = history["resets"]
    assert entry["scope"]["canonical_id"] == "openai/gpt-5.4/cli"
    assert entry["pre_reset"] == []
    assert entry["changed"] is False


def test_profiles_reset_command_rejects_legacy_alias_input(capsys, tmp_path):
    project_root = _write_profiles(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "profiles",
            "reset",
            "--project-root",
            str(project_root),
            "--model",
            "claude-sonnet",
        ]
    )

    assert cmd_profiles(args) == 2
    stderr = capsys.readouterr().err
    assert "legacy alias" in stderr
    assert CANONICAL_ID in stderr


def test_profiles_list_shows_zeroed_bucket_after_reset(capsys, tmp_path):
    project_root = _write_profiles(tmp_path)
    parser = build_parser()
    reset_args = parser.parse_args(
        [
            "profiles",
            "reset",
            "--project-root",
            str(project_root),
            "--model",
            CANONICAL_ID,
            "--role",
            "dev",
            "--complexity",
            "small",
        ]
    )
    assert cmd_profiles(reset_args) == 0

    list_args = parser.parse_args(
        [
            "profiles",
            "list",
            "--project-root",
            str(project_root),
            "--model",
            CANONICAL_ID,
            "--role",
            "dev",
        ]
    )
    assert cmd_profiles(list_args) == 0

    stdout = capsys.readouterr().out
    assert "anthropic/sonnet/cli" in stdout
    assert "dev" in stdout
    assert "small" in stdout
    assert "anthropic/sonnet/cli  dev   small" in stdout
    assert "0          0     —" in stdout
