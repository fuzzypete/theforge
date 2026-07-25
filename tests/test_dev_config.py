"""Tests for top-level dev policy parsing in forge.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.config import load_config


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


def test_dev_p2_policy_defaults_to_in_scope(tmp_path: Path) -> None:
    cfg = load_config(_write_config({}, tmp_path))
    assert cfg.dev.p2_policy == "in_scope"


@pytest.mark.parametrize("policy", ["in_scope", "all", "p1_only"])
def test_dev_p2_policy_accepts_supported_values(tmp_path: Path, policy: str) -> None:
    cfg = load_config(_write_config({"dev": {"p2_policy": policy}}, tmp_path))
    assert cfg.dev.p2_policy == policy


def test_dev_p2_policy_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dev.p2_policy"):
        load_config(_write_config({"dev": {"p2_policy": "later"}}, tmp_path))


def test_dev_section_must_be_mapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="forge.yaml 'dev' section must be a mapping"):
        load_config(_write_config({"dev": "all"}, tmp_path))
