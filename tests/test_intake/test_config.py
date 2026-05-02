"""Tests for forge.yaml ``intake:`` parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.config import load_config


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


def test_intake_defaults_when_absent(tmp_path):
    cfg = load_config(_write_config({}, tmp_path))
    assert cfg.intake.grooming is False
    assert cfg.intake.auto_fix is False
    assert cfg.intake.auto_fix_mode == "comment"


def test_intake_explicit_values(tmp_path):
    cfg = load_config(
        _write_config(
            {
                "intake": {
                    "grooming": True,
                    "auto_fix": True,
                    "auto_fix_mode": "edit",
                }
            },
            tmp_path,
        )
    )
    assert cfg.intake.grooming is True
    assert cfg.intake.auto_fix is True
    assert cfg.intake.auto_fix_mode == "edit"


def test_intake_invalid_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="auto_fix_mode"):
        load_config(_write_config({"intake": {"auto_fix_mode": "pr"}}, tmp_path))


def test_intake_invalid_grooming_type_rejected(tmp_path):
    with pytest.raises(ValueError, match="grooming"):
        load_config(_write_config({"intake": {"grooming": "yes-please"}}, tmp_path))
