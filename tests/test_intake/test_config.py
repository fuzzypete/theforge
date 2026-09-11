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
    assert cfg.intake.semantic_review == "off"


def test_intake_explicit_values(tmp_path):
    cfg = load_config(
        _write_config(
            {
                "intake": {
                    "grooming": True,
                    "auto_fix": True,
                    "auto_fix_mode": "edit",
                    "semantic_review": "required",
                }
            },
            tmp_path,
        )
    )
    assert cfg.intake.grooming is True
    assert cfg.intake.auto_fix is True
    assert cfg.intake.auto_fix_mode == "edit"
    assert cfg.intake.semantic_review == "required"


def test_intake_accepts_documented_unquoted_semantic_review_off(tmp_path):
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("intake:\n  semantic_review: off\n", encoding="utf-8")

    cfg = load_config(config_path)

    assert cfg.intake.semantic_review == "off"


def test_intake_invalid_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="auto_fix_mode"):
        load_config(_write_config({"intake": {"auto_fix_mode": "pr"}}, tmp_path))


def test_intake_invalid_semantic_review_rejected(tmp_path):
    with pytest.raises(ValueError, match="semantic_review"):
        load_config(_write_config({"intake": {"semantic_review": "advisory"}}, tmp_path))


def test_intake_rejects_boolean_true_semantic_review(tmp_path):
    with pytest.raises(ValueError, match="quoted string"):
        load_config(_write_config({"intake": {"semantic_review": True}}, tmp_path))


def test_intake_invalid_grooming_type_rejected(tmp_path):
    with pytest.raises(ValueError, match="grooming"):
        load_config(_write_config({"intake": {"grooming": "yes-please"}}, tmp_path))
