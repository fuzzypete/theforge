"""The required merge check must enforce the same standard as the story gate.

Regression guard for #1945: CI ran an independently composed `make lint` +
`make test` pair while the story gate ran `make gate`, which additionally runs
`forge index` and `forge check-story-config` and scrubs the environment. Neither
was a superset of the other, so a commit that broke the project's own config
contract passed CI while every sprint's baseline gate failed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ci_gate_job() -> dict:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    return workflow["jobs"]["gate"]


def _run_commands() -> list[str]:
    return [step["run"] for step in _ci_gate_job()["steps"] if "run" in step]


def _run_lines() -> list[str]:
    return [line.strip() for command in _run_commands() for line in command.splitlines()]


def _story_gate_command() -> str:
    config = yaml.safe_load((REPO_ROOT / "forge.yaml").read_text())
    return config["validation"]["gate_command"]


def test_ci_runs_the_story_gate_command() -> None:
    """CI invokes exactly the command forge.yaml names as the story gate."""
    assert _story_gate_command() in _run_lines()


def test_ci_does_not_substitute_a_narrower_command_pair() -> None:
    """`make lint` / `make test` omit the forge preconditions and env scrubbing."""
    lines = _run_lines()
    assert "make lint" not in lines
    assert "make test" not in lines


def test_ci_installs_into_the_venv_the_gate_resolves_from() -> None:
    """`make gate` calls .venv/bin/forge and pins PATH=".venv/bin:$PATH"."""
    commands = "\n".join(_run_commands())
    assert "python -m venv .venv" in commands
    assert '.venv/bin/pip install -e ".[all,dev]"' in commands


def test_gate_target_carries_the_forge_preconditions() -> None:
    """The target CI now runs is the one that enforces the config contract."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    gate_block = makefile[makefile.index("gate:\n") : makefile.index("gate-strict:")]
    assert "forge index" in gate_block
    assert "forge check-story-config" in gate_block
    assert "$(SCRUBBED_GATE_CMD)" in gate_block
