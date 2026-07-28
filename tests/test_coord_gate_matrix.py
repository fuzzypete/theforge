"""The story gate runs every declared interpreter and passes only if all do.

The defect (#1945): the gate that cleared a story ran one interpreter while
landing was governed by a multi-version required-check matrix, so a commit that
broke another version was reported DONE and only failed after the PR opened.
These tests pin the aggregate: one failing leg fails the whole gate.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from coord_test_helpers import _make_config

from theforge.coordinator.gate import run_gate_full
from theforge.task import TaskStory


def _matrix_config(tmp_path: Path, gate_command: str, versions: tuple[str, ...]):
    config = _make_config(tmp_path)
    return replace(
        config,
        validation=replace(
            config.validation,
            gate_command=gate_command,
            python_versions=versions,
        ),
    )


@pytest.fixture
def all_interpreters_present(monkeypatch: pytest.MonkeyPatch):
    """Pretend every pythonX.Y the matrix asks for exists on PATH."""
    monkeypatch.setattr(
        "theforge.coordinator.gate.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )


class TestMatrixAggregation:
    def test_all_legs_pass_yields_pass(self, tmp_path: Path, all_interpreters_present) -> None:
        config = _matrix_config(tmp_path, "echo leg {python_version}", ("3.11", "3.12", "3.13"))

        decision, error, output_tail, _cmd, exit_code = run_gate_full(config, tmp_path, task=None)

        assert decision == "PASS"
        assert error is None
        assert exit_code == 0
        for version in ("3.11", "3.12", "3.13"):
            assert f"leg {version}" in output_tail

    def test_one_failing_leg_fails_the_whole_gate(
        self, tmp_path: Path, all_interpreters_present
    ) -> None:
        # Passes on 3.12, fails on 3.11 — exactly the shape CI caught and the
        # single-interpreter story gate did not.
        cmd = 'test "{python_version}" = "3.12" || (echo broken on {python_version}; exit 1)'
        config = _matrix_config(tmp_path, cmd, ("3.12", "3.11"))

        decision, error, output_tail, resolved_cmd, exit_code = run_gate_full(
            config, tmp_path, task=None
        )

        assert decision == "FAIL"
        assert error is None
        assert exit_code == 1
        assert "broken on 3.11" in output_tail
        assert "3.11" in resolved_cmd

    def test_run_stops_at_the_first_failing_leg(
        self, tmp_path: Path, all_interpreters_present
    ) -> None:
        marker = tmp_path / "ran"
        cmd = (
            f'echo {{python_version}} >> "{marker}"; test "{{python_version}}" != "3.11" || exit 1'
        )
        config = _matrix_config(tmp_path, cmd, ("3.11", "3.12", "3.13"))

        decision, _error, _tail, _cmd, _exit = run_gate_full(config, tmp_path, task=None)

        assert decision == "FAIL"
        assert marker.read_text(encoding="utf-8").split() == ["3.11"]

    def test_every_declared_version_is_actually_run(
        self, tmp_path: Path, all_interpreters_present
    ) -> None:
        marker = tmp_path / "ran"
        config = _matrix_config(
            tmp_path, f'echo {{python_version}} >> "{marker}"', ("3.11", "3.12", "3.13")
        )

        decision, _error, _tail, _cmd, _exit = run_gate_full(config, tmp_path, task=None)

        assert decision == "PASS"
        assert marker.read_text(encoding="utf-8").split() == ["3.11", "3.12", "3.13"]


class TestMissingInterpreter:
    def test_missing_interpreter_is_infrastructure_error_not_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A host without python3.11 must not silently prove less than it claims.
        monkeypatch.setattr(
            "theforge.coordinator.gate.shutil.which",
            lambda name: None if name == "python3.11" else f"/usr/bin/{name}",
        )
        config = _matrix_config(tmp_path, "echo {python_version}", ("3.11", "3.12"))

        decision, error, _tail, _cmd, exit_code = run_gate_full(config, tmp_path, task=None)

        assert decision is None
        assert error is not None
        assert "python3.11" in error
        assert exit_code is None

    def test_missing_interpreter_does_not_run_any_leg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = tmp_path / "ran"
        monkeypatch.setattr(
            "theforge.coordinator.gate.shutil.which",
            lambda name: None if name == "python3.13" else f"/usr/bin/{name}",
        )
        config = _matrix_config(
            tmp_path, f'echo {{python_version}} >> "{marker}"', ("3.11", "3.13")
        )

        decision, error, _tail, _cmd, _exit = run_gate_full(config, tmp_path, task=None)

        assert decision is None and error is not None
        assert not marker.exists()


class TestMatrixTelemetry:
    def test_output_digest_receives_exactly_one_hash_covering_all_legs(
        self, tmp_path: Path, all_interpreters_present
    ) -> None:
        config = _matrix_config(tmp_path, "echo leg {python_version}", ("3.11", "3.12"))
        first: list[str] = []
        second: list[str] = []

        run_gate_full(config, tmp_path, task=None, output_digest=first)
        run_gate_full(config, tmp_path, task=None, output_digest=second)

        assert len(first) == 1
        assert first == second

        other = _matrix_config(tmp_path, "echo leg {python_version} v2", ("3.11", "3.12"))
        changed: list[str] = []
        run_gate_full(other, tmp_path, task=None, output_digest=changed)
        assert changed != first

    def test_each_leg_writes_its_own_trace(self, tmp_path: Path, all_interpreters_present) -> None:
        config = _matrix_config(tmp_path, "echo leg {python_version}", ("3.11", "3.12"))

        run_gate_full(config, tmp_path, task=None, iter_num=2)

        traces = tmp_path / ".forge" / "traces"
        assert "leg 3.11" in (traces / "2-gate-py3.11.txt").read_text(encoding="utf-8")
        assert "leg 3.12" in (traces / "2-gate-py3.12.txt").read_text(encoding="utf-8")


class TestTraceAttribution:
    """The trace a terminal outcome names must be the file the verdict came from."""

    def test_reports_every_leg_trace_in_run_order(
        self, tmp_path: Path, all_interpreters_present
    ) -> None:
        config = _matrix_config(tmp_path, "echo leg {python_version}", ("3.11", "3.12"))
        traces: list[str] = []

        run_gate_full(config, tmp_path, task=None, iter_num=2, trace_names=traces)

        assert traces == [".forge/traces/2-gate-py3.11.txt", ".forge/traces/2-gate-py3.12.txt"]

    def test_failing_leg_trace_is_last_and_exists_on_disk(
        self, tmp_path: Path, all_interpreters_present
    ) -> None:
        # A python3.12 failure must not be reported against `2-gate.txt`, which
        # a matrix run never writes.
        config = _matrix_config(
            tmp_path, "test {python_version} != 3.12", ("3.11", "3.12", "3.13")
        )
        traces: list[str] = []

        decision, _error, _tail, _cmd, _exit_code = run_gate_full(
            config, tmp_path, task=None, iter_num=2, trace_names=traces
        )

        assert decision == "FAIL"
        assert traces[-1] == ".forge/traces/2-gate-py3.12.txt"
        assert (tmp_path / traces[-1]).exists()

    def test_no_trace_reported_without_an_iteration(
        self, tmp_path: Path, all_interpreters_present
    ) -> None:
        config = _matrix_config(tmp_path, "echo leg {python_version}", ("3.11",))
        traces: list[str] = []

        run_gate_full(config, tmp_path, task=None, iter_num=None, trace_names=traces)

        assert traces == []

    def test_single_leg_gate_reports_the_legacy_trace(self, tmp_path: Path) -> None:
        config = _matrix_config(tmp_path, "echo single", ())
        traces: list[str] = []

        run_gate_full(config, tmp_path, task=None, iter_num=1, trace_names=traces)

        assert traces == [".forge/traces/1-gate.txt"]


class TestSingleRunGateUnchanged:
    def test_no_declared_versions_runs_once_and_writes_the_legacy_trace(
        self, tmp_path: Path
    ) -> None:
        config = _matrix_config(tmp_path, "echo single", ())

        decision, error, output_tail, resolved_cmd, exit_code = run_gate_full(
            config, tmp_path, task=None, iter_num=1
        )

        assert (decision, error, exit_code) == ("PASS", None, 0)
        assert resolved_cmd == "echo single"
        assert "single" in output_tail
        assert (tmp_path / ".forge" / "traces" / "1-gate.txt").exists()

    def test_gate_override_runs_single_leg(self, tmp_path: Path, all_interpreters_present) -> None:
        # An override already replaces the configured command wholesale; the
        # matrix is a property of that command, so it does not apply.
        marker = tmp_path / "ran"
        config = _matrix_config(tmp_path, "echo {python_version}", ("3.11", "3.12", "3.13"))
        spec = tmp_path / "spec.md"
        spec.write_text("# Spec\n", encoding="utf-8")
        task = TaskStory(
            name="Override",
            story_path=spec,
            slug="override",
            gate_override=f'echo override >> "{marker}"',
        )

        decision, _error, _tail, resolved_cmd, _exit = run_gate_full(config, tmp_path, task=task)

        assert decision == "PASS"
        assert "override" in resolved_cmd
        assert marker.read_text(encoding="utf-8").split() == ["override"]
