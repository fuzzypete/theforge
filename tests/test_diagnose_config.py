"""Config-loader and CLI-registration tests for the diagnose subsystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.config import DiagnoseConfig, load_config


def _write_config(path: Path, raw: dict) -> Path:
    import yaml

    p = path / "forge.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_default_diagnose_config_when_block_absent(tmp_path: Path):
    cfg_path = _write_config(tmp_path, {"project": "test"})
    config = load_config(cfg_path)
    assert isinstance(config.diagnose, DiagnoseConfig)
    assert config.diagnose.output_destination == "body_section"
    assert config.diagnose.budget_usd > 0
    assert config.diagnose.timeout_seconds > 0


def test_diagnose_block_overrides_defaults(tmp_path: Path):
    cfg_path = _write_config(
        tmp_path,
        {
            "project": "test",
            "diagnose": {
                "output_destination": "body_section",
                "budget_usd": 3.5,
                "timeout_seconds": 1200,
                "autonomous_default": False,
            },
        },
    )
    config = load_config(cfg_path)
    assert config.diagnose.output_destination == "body_section"
    assert config.diagnose.budget_usd == 3.5
    assert config.diagnose.timeout_seconds == 1200
    assert config.diagnose.autonomous_default is False


def test_invalid_destination_rejected(tmp_path: Path):
    cfg_path = _write_config(
        tmp_path,
        {"project": "test", "diagnose": {"output_destination": "nope"}},
    )
    with pytest.raises(ValueError, match="output_destination"):
        load_config(cfg_path)


def test_invalid_budget_rejected(tmp_path: Path):
    cfg_path = _write_config(
        tmp_path,
        {"project": "test", "diagnose": {"budget_usd": -1}},
    )
    with pytest.raises(ValueError, match="budget_usd"):
        load_config(cfg_path)


def test_cli_registers_diagnose_command():
    from theforge.cli.main import build_parser

    parser = build_parser()
    # Smoke: parsing a minimal diagnose invocation should not raise.
    args = parser.parse_args(["diagnose", "--issue", "42"])
    assert args.command == "diagnose"
    assert args.issue == ["42"]
