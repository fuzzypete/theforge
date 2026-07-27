"""Loader tests for ``validation.python_versions`` — the gate's interpreter matrix.

The list decides which environment surface the story gate proves, so loading is
strict: a malformed entry that degraded to "run once under whatever interpreter
is around" would restore the #1945 defect while looking configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.config import load_config


def _write_config(validation: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump({"validation": validation}), encoding="utf-8")
    return config_path


def _load(validation: dict, tmp_path: Path):
    return load_config(_write_config(validation, tmp_path))


class TestValidPythonVersions:
    def test_declared_matrix_is_normalized_to_a_tuple(self, tmp_path: Path) -> None:
        config = _load(
            {
                "gate_command": "make gate-py PY={python_version}",
                "python_versions": ["3.11", "3.12", "3.13"],
            },
            tmp_path,
        )
        assert config.validation.python_versions == ("3.11", "3.12", "3.13")

    def test_declared_order_is_preserved(self, tmp_path: Path) -> None:
        config = _load(
            {
                "gate_command": "make gate-py PY={python_version}",
                "python_versions": ["3.13", "3.11"],
            },
            tmp_path,
        )
        assert config.validation.python_versions == ("3.13", "3.11")

    def test_omitted_key_keeps_the_legacy_single_run_gate(self, tmp_path: Path) -> None:
        config = _load({"gate_command": "make gate"}, tmp_path)
        assert config.validation.python_versions == ()


class TestInvalidPythonVersions:
    def test_empty_list_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _load(
                {"gate_command": "make gate-py PY={python_version}", "python_versions": []},
                tmp_path,
            )

    def test_scalar_string_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            _load(
                {"gate_command": "make gate-py PY={python_version}", "python_versions": "3.11"},
                tmp_path,
            )

    def test_unquoted_version_parses_as_float_and_is_rejected(self, tmp_path: Path) -> None:
        # YAML turns bare 3.11 into a float; str() of it would silently name a
        # different interpreter than the operator wrote (3.10 -> "3.1").
        with pytest.raises(ValueError, match="must be quoted strings"):
            _load(
                {"gate_command": "make gate-py PY={python_version}", "python_versions": [3.11]},
                tmp_path,
            )

    @pytest.mark.parametrize("bad", ["3", "py3.11", "3.11.2", "", "3.x"])
    def test_malformed_version_strings_are_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError, match="MAJOR.MINOR"):
            _load(
                {"gate_command": "make gate-py PY={python_version}", "python_versions": [bad]},
                tmp_path,
            )

    def test_duplicate_entries_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="duplicate entry"):
            _load(
                {
                    "gate_command": "make gate-py PY={python_version}",
                    "python_versions": ["3.12", "3.12"],
                },
                tmp_path,
            )


class TestGateCommandCrossValidation:
    def test_matrix_without_placeholder_is_rejected(self, tmp_path: Path) -> None:
        # Every leg would run the identical command — matrix coverage in name only.
        with pytest.raises(ValueError, match="no .python_version. placeholder"):
            _load(
                {"gate_command": "make gate", "python_versions": ["3.11", "3.12"]},
                tmp_path,
            )

    def test_placeholder_without_matrix_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="python_versions is not set"):
            _load({"gate_command": "make gate-py PY={python_version}"}, tmp_path)
