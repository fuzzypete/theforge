"""run_gate_full must substitute {test_target}/{slug} even when no task is
supplied (the baseline gate path), instead of leaking the literal
placeholder text into the shell command."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from coord_test_helpers import _make_config

from theforge.coordinator.gate import run_gate_full
from theforge.task import TaskStory


def test_no_task_substitutes_default_test_target_and_slug(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = replace(
        config,
        validation=replace(config.validation, gate_command="echo {test_target} {slug}"),
    )

    _decision, _error, _output_tail, resolved_gate_cmd, _exit_code = run_gate_full(
        config, tmp_path, task=None
    )

    assert "{test_target}" not in resolved_gate_cmd
    assert "{slug}" not in resolved_gate_cmd
    assert "." in resolved_gate_cmd
    assert "baseline" in resolved_gate_cmd


def test_no_task_uses_configured_default_test_target(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command="echo {test_target}",
            default_test_target="tests/",
        ),
    )

    _decision, _error, _output_tail, resolved_gate_cmd, _exit_code = run_gate_full(
        config, tmp_path, task=None
    )

    assert resolved_gate_cmd == "echo tests/"


def test_task_test_target_still_takes_precedence_over_default(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command="echo {test_target} {slug}",
            default_test_target="tests/",
        ),
    )
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n", encoding="utf-8")
    task = TaskStory(
        name="Test Task",
        story_path=spec,
        slug="my-slug",
        test_target="tests/test_foo.py",
    )

    _decision, _error, _output_tail, resolved_gate_cmd, _exit_code = run_gate_full(
        config, tmp_path, task=task
    )

    assert resolved_gate_cmd == "echo tests/test_foo.py my-slug"
