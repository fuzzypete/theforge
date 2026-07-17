"""Tests for ``forge diagnose --parallel`` concurrency.

Covers:
- ``_run_diagnoses`` serial vs. parallel dispatch and overall-OK aggregation
- worker-cap discipline (bounded concurrency, no unbounded spawn)
- per-issue log tagging via the thread-local worker slug
- CLI arg wiring and interactive-mode downgrade to serial
"""

from __future__ import annotations

import argparse
import threading
import time
from unittest.mock import patch

from theforge.cli.diagnose import _run_diagnoses, cmd_diagnose, register_parser
from theforge.coordinator.diagnose_flow import _emit_dry_run
from theforge.coordinator.log_tee import get_worker_slug, set_worker_slug
from theforge.diagnose_types import DiagnosePhase, DiagnoseResult, DiagnoseState


def _result(number: int, *, success: bool = True) -> DiagnoseResult:
    state = DiagnoseState(issue_number=number, phase=DiagnosePhase.DONE)
    state.agent_cost_usd = 0.0
    state.agent_duration_s = 0.1
    return DiagnoseResult(success=success, state=state, message="ok")


# ── _run_diagnoses dispatcher ─────────────────────────────────────────


class TestRunDiagnoses:
    def test_serial_processes_every_issue_in_order(self):
        seen: list[int] = []

        def worker(number: int, *, tagged: bool) -> bool:
            assert tagged is False  # serial path never tags
            seen.append(number)
            return True

        ok = _run_diagnoses([1, 2, 3], worker, effective_parallel=1)
        assert ok is True
        assert seen == [1, 2, 3]

    def test_single_issue_stays_serial_even_with_parallel(self):
        def worker(number: int, *, tagged: bool) -> bool:
            assert tagged is False
            return True

        assert _run_diagnoses([7], worker, effective_parallel=4) is True

    def test_overall_ok_false_when_any_issue_fails(self):
        def worker(number: int, *, tagged: bool) -> bool:
            return number != 2

        assert _run_diagnoses([1, 2, 3], worker, effective_parallel=1) is False

    def test_parallel_processes_every_issue(self):
        seen: set[int] = set()
        lock = threading.Lock()

        def worker(number: int, *, tagged: bool) -> bool:
            assert tagged is True  # parallel path tags each worker
            with lock:
                seen.add(number)
            return True

        ok = _run_diagnoses([10, 11, 12, 13], worker, effective_parallel=3)
        assert ok is True
        assert seen == {10, 11, 12, 13}

    def test_parallel_respects_worker_cap(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def worker(number: int, *, tagged: bool) -> bool:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return True

        _run_diagnoses([1, 2, 3, 4, 5, 6], worker, effective_parallel=2)
        assert peak <= 2

    def test_parallel_worker_exception_marks_failure_not_abort(self):
        completed: set[int] = set()
        lock = threading.Lock()

        def worker(number: int, *, tagged: bool) -> bool:
            if number == 2:
                raise RuntimeError("boom")
            with lock:
                completed.add(number)
            return True

        ok = _run_diagnoses([1, 2, 3], worker, effective_parallel=3)
        assert ok is False
        # The crash of #2 must not prevent #1 and #3 from finishing.
        assert completed == {1, 3}

    def test_parallel_worker_sets_thread_local_slug(self):
        slugs: dict[int, str] = {}

        def worker(number: int, *, tagged: bool) -> bool:
            # Mirror what _diagnose_one does so we exercise the real tagging path.
            from theforge.coordinator.log_tee import set_worker_slug

            if tagged:
                set_worker_slug(f"#{number}")
            slugs[number] = get_worker_slug()
            return True

        _run_diagnoses([100, 101], worker, effective_parallel=2)
        assert slugs[100] == "#100"
        assert slugs[101] == "#101"


# ── Dry-run output tagging ────────────────────────────────────────────


class TestEmitDryRun:
    def test_no_slug_prints_verbatim(self, capsys):
        try:
            set_worker_slug("")
            _emit_dry_run("line one\nline two")
        finally:
            set_worker_slug("")
        assert capsys.readouterr().out == "line one\nline two\n"

    def test_slug_prefixes_every_line(self, capsys):
        try:
            set_worker_slug("#42")
            _emit_dry_run("## Diagnosis\n\nconfirmed cause")
        finally:
            set_worker_slug("")
        out = capsys.readouterr().out
        assert out == "[#42] ## Diagnosis\n[#42] \n[#42] confirmed cause\n"


# ── CLI wiring ────────────────────────────────────────────────────────


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register_parser(sub)
    return parser.parse_args(["diagnose", *argv])


class TestParserArg:
    def test_parallel_defaults_to_none(self):
        args = _parse(["--issue", "1"])
        assert args.parallel is None

    def test_parallel_parsed_as_int(self):
        args = _parse(["--issue", "1", "--parallel", "3"])
        assert args.parallel == 3


class TestCmdDiagnose:
    def _args(self, **over) -> argparse.Namespace:
        base = dict(
            issue=["1,2,3"],
            config=None,
            output_destination=None,
            interactive=False,
            autonomous=True,
            dry_run=False,
            parallel=None,
            verbose=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _patched_config(self):
        class _Diag:
            output_destination = "body_section"
            autonomous_default = True

        class _Cfg:
            project_root = "/tmp/x"
            diagnose = _Diag()

        return _Cfg()

    def test_parallel_diagnoses_all_issues(self, tmp_path):
        cfg_path = tmp_path / "forge.yaml"
        cfg_path.write_text("x")
        calls: list[int] = []
        lock = threading.Lock()

        def fake_flow(*, issue_number, **_):
            with lock:
                calls.append(issue_number)
            return _result(issue_number)

        with (
            patch("theforge.cli.diagnose._find_config", return_value=cfg_path),
            patch("theforge.cli.diagnose.load_config", return_value=self._patched_config()),
            patch("theforge.cli.diagnose.run_diagnose_flow", side_effect=fake_flow) as flow,
        ):
            rc = cmd_diagnose(self._args(parallel=3))

        assert rc == 0
        assert flow.call_count == 3
        assert sorted(calls) == [1, 2, 3]

    def test_invalid_parallel_returns_error(self, tmp_path):
        cfg_path = tmp_path / "forge.yaml"
        cfg_path.write_text("x")
        with (
            patch("theforge.cli.diagnose._find_config", return_value=cfg_path),
            patch("theforge.cli.diagnose.load_config", return_value=self._patched_config()),
            patch("theforge.cli.diagnose.run_diagnose_flow") as flow,
        ):
            rc = cmd_diagnose(self._args(parallel=0))
        assert rc == 1
        flow.assert_not_called()

    def test_dry_run_parallel_tags_multiline_output(self, tmp_path, capsys):
        # The dry-run markdown each worker prints must be attributable per issue
        # under --parallel; every emitted line carries its issue's worker slug.
        cfg_path = tmp_path / "forge.yaml"
        cfg_path.write_text("x")

        def fake_flow(*, issue_number, dry_run, **_):
            assert dry_run is True
            # Mirror _land_artifact's dry-run path: the worker slug is live here.
            _emit_dry_run(f"## Diagnosis #{issue_number}\n\nbody line")
            return _result(issue_number)

        with (
            patch("theforge.cli.diagnose._find_config", return_value=cfg_path),
            patch("theforge.cli.diagnose.load_config", return_value=self._patched_config()),
            patch("theforge.cli.diagnose.run_diagnose_flow", side_effect=fake_flow),
        ):
            rc = cmd_diagnose(self._args(dry_run=True, parallel=3))

        assert rc == 0
        out_lines = capsys.readouterr().out.splitlines()
        # Every non-empty dry-run line is prefixed with some issue's slug.
        content_lines = [ln for ln in out_lines if ln.strip("[]# ")]
        assert content_lines, "expected dry-run markdown on stdout"
        for ln in content_lines:
            assert ln.startswith("[#1] ") or ln.startswith("[#2] ") or ln.startswith("[#3] ")
        # Each issue's own section is present and self-tagged.
        for n in (1, 2, 3):
            assert f"[#{n}] ## Diagnosis #{n}" in out_lines

    def test_interactive_parallel_downgrades_to_serial(self, tmp_path, capsys):
        cfg_path = tmp_path / "forge.yaml"
        cfg_path.write_text("x")

        def fake_flow(*, issue_number, **_):
            # In serial mode the worker slug is never set.
            assert get_worker_slug() == ""
            return _result(issue_number)

        with (
            patch("theforge.cli.diagnose._find_config", return_value=cfg_path),
            patch("theforge.cli.diagnose.load_config", return_value=self._patched_config()),
            patch("theforge.cli.diagnose.run_diagnose_flow", side_effect=fake_flow),
        ):
            rc = cmd_diagnose(self._args(interactive=True, autonomous=False, parallel=4))

        assert rc == 0
        assert "ignored in interactive mode" in capsys.readouterr().err
