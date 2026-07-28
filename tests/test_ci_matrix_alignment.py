"""The CI required-check matrix and the local story gate must cover the same
interpreters.

This is the drift guard for #1945. The story gate and the required merge checks
disagreeing about which Pythons a commit must satisfy is what let a story be
reported complete while its PR sat blocked. Because this test runs inside the
gate *and* inside CI, either side moving alone fails immediately instead of
surfacing as a post-hoc merge failure.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.config import PYTHON_VERSION_PLACEHOLDER

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
FORGE_YAML = REPO_ROOT / "forge.yaml"


def _ci_gate_matrix() -> list[str]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return list(workflow["jobs"]["gate"]["strategy"]["matrix"]["python-version"])


def _forge_validation() -> dict:
    return yaml.safe_load(FORGE_YAML.read_text(encoding="utf-8"))["validation"]


def test_ci_matrix_matches_forge_validation_python_versions() -> None:
    assert _forge_validation()["python_versions"] == _ci_gate_matrix()


def test_forge_gate_command_carries_the_python_version_placeholder() -> None:
    # Without it, load_config rejects the config outright; asserting here names
    # the coupling so a future gate_command edit fails with a clear reason.
    assert PYTHON_VERSION_PLACEHOLDER in _forge_validation()["gate_command"]


def test_ci_matrix_versions_are_quoted_strings() -> None:
    # Unquoted 3.10 is a YAML float that round-trips to "3.1" — a different
    # interpreter than the one the workflow means.
    assert all(isinstance(v, str) for v in _ci_gate_matrix())
