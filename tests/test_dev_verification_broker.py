"""Unit tests for the coordinator-owned dev verification broker (ADR-0007 / #2050).

Covers the request/response protocol, the fail-closed budget, refusal of names
the project never declared, and the load-time config validation that decides
what may run outside the dev sandbox at all.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from coord_test_helpers import patch_gate_shell

from theforge.config.load import load_config
from theforge.config.types import DevVerificationCommand
from theforge.coordinator.dev_verification import DevVerificationBroker
from theforge.task.dev_prompts import render_verification_section

VERIFY_WATCH = DevVerificationCommand(
    name="verify-watch",
    command="xcodebuild -scheme Watch test",
    timeout=900,
    output_tail_chars=100,
)


def _broker(tmp_path: Path, *, commands=(VERIFY_WATCH,), max_requests: int = 10):
    return DevVerificationBroker(
        workspace_path=tmp_path,
        commands=commands,
        iteration=1,
        max_requests=max_requests,
    )


def _request(broker: DevVerificationBroker, request_id: str, body) -> None:
    """Write a request file the way the prompt instructs the agent to: atomically."""
    tmp = broker.request_dir / f"{request_id}.json.tmp"
    tmp.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    tmp.rename(broker.request_dir / f"{request_id}.json")


def _response(broker: DevVerificationBroker, request_id: str) -> dict:
    return json.loads((broker.response_dir / f"{request_id}.json").read_text(encoding="utf-8"))


class TestDeclaredCommandExecution:
    def test_declared_name_runs_the_configured_command_verbatim(self, tmp_path):
        broker = _broker(tmp_path)
        _request(broker, "r1", {"command": "verify-watch"})

        with patch_gate_shell(
            return_value=(True, "** TEST SUCCEEDED **", 0, False),
        ) as mock_shell:
            broker.poll_once()

        # The command executed is the *configured* string, not anything the
        # request could influence: the agent supplied only a name.
        assert mock_shell.call_args.args[0] == "xcodebuild -scheme Watch test"
        assert mock_shell.call_args.args[1] == tmp_path
        assert mock_shell.call_args.kwargs["timeout"] == 900

        response = _response(broker, "r1")
        assert response["accepted"] is True
        assert response["command"] == "verify-watch"
        assert response["exit_code"] == 0
        assert response["timed_out"] is False
        assert response["success"] is True
        assert "TEST SUCCEEDED" in response["output_tail"]
        assert response["trace_path"] == ".forge/traces/verify-iter1-r1.txt"
        assert (tmp_path / response["trace_path"]).read_text() == "** TEST SUCCEEDED **"

    def test_failing_command_reports_the_real_exit_code(self, tmp_path):
        broker = _broker(tmp_path)
        _request(broker, "r1", {"command": "verify-watch"})

        with patch_gate_shell(
            return_value=(False, "error: cannot find 'foo' in scope", 65, False),
        ):
            broker.poll_once()

        response = _response(broker, "r1")
        assert response["accepted"] is True
        assert response["success"] is False
        assert response["exit_code"] == 65
        assert "cannot find" in response["output_tail"]

    def test_timeout_is_reported_rather_than_read_as_a_pass(self, tmp_path):
        broker = _broker(tmp_path)
        _request(broker, "r1", {"command": "verify-watch"})

        with patch_gate_shell(
            return_value=(False, "TIMEOUT", None, True),
        ):
            broker.poll_once()

        response = _response(broker, "r1")
        assert response["timed_out"] is True
        assert response["success"] is False

    def test_long_output_is_tailed_and_flagged_with_the_full_trace_kept(self, tmp_path):
        broker = _broker(tmp_path)
        _request(broker, "r1", {"command": "verify-watch"})
        full = "x" * 5000

        with patch_gate_shell(
            return_value=(True, full, 0, False),
        ):
            broker.poll_once()

        response = _response(broker, "r1")
        assert len(response["output_tail"]) == 100
        assert response["output_truncated"] is True
        assert (tmp_path / response["trace_path"]).read_text() == full

    def test_each_request_is_served_exactly_once(self, tmp_path):
        broker = _broker(tmp_path)
        _request(broker, "r1", {"command": "verify-watch"})

        with patch_gate_shell(
            return_value=(True, "ok", 0, False),
        ) as mock_shell:
            broker.poll_once()
            broker.poll_once()
            broker.poll_once()

        assert mock_shell.call_count == 1
        assert len(broker.records()) == 1


class TestRefusals:
    def test_undeclared_command_name_is_refused_without_executing(self, tmp_path):
        broker = _broker(tmp_path)
        _request(broker, "r1", {"command": "rm-rf-everything"})

        with patch_gate_shell() as mock_shell:
            broker.poll_once()

        assert mock_shell.call_count == 0
        response = _response(broker, "r1")
        assert response["accepted"] is False
        assert response["refusal_reason"] == "unknown_command"
        assert response["exit_code"] is None

    def test_a_request_cannot_smuggle_shell_text(self, tmp_path):
        """Only ``command`` is read, and only as a declared *name*."""
        broker = _broker(tmp_path)
        _request(
            broker,
            "r1",
            {"command": "verify-watch; rm -rf /", "argv": ["--evil"], "shell": "whoami"},
        )

        with patch_gate_shell() as mock_shell:
            broker.poll_once()

        assert mock_shell.call_count == 0
        assert _response(broker, "r1")["refusal_reason"] == "unknown_command"

    def test_malformed_request_body_is_refused(self, tmp_path):
        broker = _broker(tmp_path)
        _request(broker, "r1", ["verify-watch"])

        with patch_gate_shell() as mock_shell:
            broker.poll_once()

        assert mock_shell.call_count == 0
        assert _response(broker, "r1")["refusal_reason"] == "malformed_request"

    def test_unparseable_request_is_retried_before_being_refused(self, tmp_path):
        """A half-written file must not be refused on the first glimpse of it."""
        broker = _broker(tmp_path)
        (broker.request_dir / "r1.json").write_text('{"command": "verify-w', encoding="utf-8")

        with patch_gate_shell() as mock_shell:
            broker.poll_once()
            assert not (broker.response_dir / "r1.json").exists()
            # The writer finishes before the retry budget runs out.
            (broker.request_dir / "r1.json").write_text(
                '{"command": "verify-watch"}', encoding="utf-8"
            )
            mock_shell.return_value = (True, "ok", 0, False)
            broker.poll_once()

        assert _response(broker, "r1")["accepted"] is True

    def test_persistently_unparseable_request_is_eventually_refused(self, tmp_path):
        broker = _broker(tmp_path)
        (broker.request_dir / "r1.json").write_text("not json at all", encoding="utf-8")

        with patch_gate_shell() as mock_shell:
            for _ in range(20):
                broker.poll_once()

        assert mock_shell.call_count == 0
        assert _response(broker, "r1")["refusal_reason"] == "malformed_request"


class TestRequestBudget:
    def test_budget_is_fail_closed_once_exhausted(self, tmp_path):
        broker = _broker(tmp_path, max_requests=2)
        for i in range(4):
            _request(broker, f"r{i}", {"command": "verify-watch"})

        with patch_gate_shell(
            return_value=(True, "ok", 0, False),
        ) as mock_shell:
            broker.poll_once()

        assert mock_shell.call_count == 2
        assert _response(broker, "r0")["accepted"] is True
        assert _response(broker, "r1")["accepted"] is True
        for refused in ("r2", "r3"):
            response = _response(broker, refused)
            assert response["accepted"] is False
            assert response["refusal_reason"] == "request_limit_exceeded"

    def test_refused_requests_spend_the_budget_too(self, tmp_path):
        """A loop of bad names cannot buy unbounded coordinator execution."""
        broker = _broker(tmp_path, max_requests=2)
        _request(broker, "r0", {"command": "nope"})
        _request(broker, "r1", {"command": "nope"})
        _request(broker, "r2", {"command": "verify-watch"})

        with patch_gate_shell() as mock_shell:
            broker.poll_once()

        assert mock_shell.call_count == 0
        assert _response(broker, "r2")["refusal_reason"] == "request_limit_exceeded"

    def test_budget_does_not_reset_across_transport_retries(self, tmp_path):
        """The broker spans the whole iteration, so start/stop cycles share a budget."""
        broker = _broker(tmp_path, max_requests=1)
        _request(broker, "r0", {"command": "verify-watch"})
        with patch_gate_shell(
            return_value=(True, "ok", 0, False),
        ):
            broker.poll_once()

        # A run_agent re-attempt inside the same iteration: same broker instance.
        _request(broker, "r1", {"command": "verify-watch"})
        with patch_gate_shell() as mock_shell:
            broker.poll_once()

        assert mock_shell.call_count == 0
        assert _response(broker, "r1")["refusal_reason"] == "request_limit_exceeded"


class TestShutdownWithCommandInFlight:
    """A declared command must never outlive the dev iteration that asked for it.

    An unconfined build still writing to the worktree would race the
    coordinator's own authoritative gate, and a request that simply vanishes
    leaves no audit record of an unconfined execution the coordinator started.
    """

    def test_in_flight_command_is_killed_and_recorded(self, tmp_path):
        broker = _broker(tmp_path)
        started = threading.Event()
        release = threading.Event()
        killed: list[object] = []

        def slow_command(cmd, cwd, **kwargs):
            """Stand in for a long build: blocks until its 'process group' is killed."""
            proc = object()
            on_start = kwargs.get("on_process_start")
            assert on_start is not None, "broker must publish a cancellable handle"
            on_start(proc)
            started.set()
            # Real work would be blocked in Popen.communicate() here; it returns
            # only once the kill lands, which is what release stands in for.
            assert release.wait(10), "command was never terminated"
            return (False, "partial build output", -9, False)

        def fake_kill(proc):
            killed.append(proc)
            release.set()
            return True

        with (
            patch_gate_shell(side_effect=slow_command),
            patch("theforge.coordinator.util._kill_process_group", side_effect=fake_kill),
        ):
            broker.start()
            _request(broker, "r1", {"command": "verify-watch"})
            assert started.wait(10), "command never started"
            # The agent has returned while the build is still running.
            broker.stop(timeout=0.2)

        assert killed, "stop() must terminate the in-flight command's process group"
        (record,) = broker.records()
        assert record["command_name"] == "verify-watch"
        assert record["cancelled"] is True
        # A kill's exit code is an artifact of the kill, not a verdict on the code.
        assert record["refusal_reason"] == "cancelled_at_iteration_end"
        response = _response(broker, "r1")
        assert response["cancelled"] is True
        assert response["success"] is False

    def test_a_command_that_finishes_inside_the_grace_is_not_cancelled(self, tmp_path):
        broker = _broker(tmp_path)

        with (
            patch_gate_shell(return_value=(True, "ok", 0, False)),
            patch("theforge.coordinator.util._kill_process_group") as mock_kill,
        ):
            broker.start()
            _request(broker, "r1", {"command": "verify-watch"})
            for _ in range(200):
                if broker.records():
                    break
                time.sleep(0.01)
            broker.stop(timeout=5)

        assert mock_kill.call_count == 0
        (record,) = broker.records()
        assert record["cancelled"] is False
        assert record["exit_code"] == 0

    def test_a_request_arriving_during_shutdown_is_refused_not_started(self, tmp_path):
        """Shutdown must not launch a minutes-long build no one is left to read."""
        broker = _broker(tmp_path)
        broker._stop_event.set()
        _request(broker, "r1", {"command": "verify-watch"})

        with patch_gate_shell() as mock_shell:
            broker.poll_once()

        assert mock_shell.call_count == 0
        response = _response(broker, "r1")
        assert response["accepted"] is False
        assert response["refusal_reason"] == "iteration_ended"
        # Refused, but still audited: the request is never simply absent.
        assert broker.records()[0]["refusal_reason"] == "iteration_ended"

    def test_a_cancelled_command_the_thread_never_records_is_backfilled(self, tmp_path):
        """Last-resort guarantee: the audit trail names every started command.

        The serving thread has to be *still running* when ``stop`` gives up on
        it, which is the whole condition under test — but it is released and
        joined before this test returns. It used to sleep 30s and be abandoned:
        the thread then woke inside whichever unrelated test that xdist worker
        had reached half a minute later, and was measured there consuming a
        patched ``time.monotonic``, failing a sprint worker-timeout test whose
        scripted clock it exhausted. Which test it landed on depended on
        worksteal scheduling, so the same commit passed or failed by luck —
        the instability this story exists to remove (#2825).
        """
        broker = _broker(tmp_path)
        started = threading.Event()
        release = threading.Event()

        def wedged_command(cmd, cwd, **kwargs):
            kwargs["on_process_start"](object())
            started.set()
            assert release.wait(10), "the wedged command was never released"
            return (False, "", None, False)

        serving: threading.Thread | None = None
        try:
            with (
                patch_gate_shell(side_effect=wedged_command),
                patch("theforge.coordinator.util._kill_process_group", return_value=True),
            ):
                broker.start()
                serving = broker._thread
                _request(broker, "r1", {"command": "verify-watch"})
                assert started.wait(10), "command never started"
                import theforge.coordinator.dev_verification as dv

                with patch.object(dv, "_CANCEL_JOIN_SECONDS", 0.2):
                    broker.stop(timeout=0.2)

            # Read before the thread is let go: the point is the record stop()
            # backfilled for a thread that had not recorded anything yet.
            (record,) = broker.records()
            assert record["request_id"] == "r1"
            assert record["cancelled"] is True
            assert record["exit_code"] is None
            assert record["refusal_reason"] == "cancelled_at_iteration_end"
        finally:
            release.set()
            if serving is not None:
                serving.join(timeout=10)
                assert not serving.is_alive(), (
                    "the serving thread must not outlive the test that started it"
                )


class TestShutdownBudgetIsTheDeclaredTimeout:
    """A declared ``timeout`` is the budget the command actually gets (#2078).

    The shutdown wait used to be a fixed 30s constant unrelated to the declared
    value, so a command configured at 1800s could never obtain more than 30s
    once the agent's turn ended — every configured value described something
    that could not happen.
    """

    def test_stop_lets_a_command_run_past_the_fixed_grace_toward_its_own_timeout(self, tmp_path):
        slow = DevVerificationCommand(
            name="check-ios", command="xcodebuild build", timeout=30, output_tail_chars=100
        )
        broker = _broker(tmp_path, commands=(slow,))
        started = threading.Event()

        def slow_command(cmd, cwd, **kwargs):
            kwargs["on_process_start"](object())
            started.set()
            # Comfortably longer than the (patched) fixed grace, comfortably
            # shorter than the command's own declared budget.
            time.sleep(0.6)
            return (True, "** BUILD SUCCEEDED **", 0, False)

        with (
            patch_gate_shell(side_effect=slow_command),
            patch("theforge.coordinator.util._kill_process_group") as mock_kill,
            patch("theforge.coordinator.dev_verification._SHUTDOWN_GRACE_SECONDS", 0.05),
        ):
            broker.start()
            _request(broker, "r1", {"command": "check-ios"})
            assert started.wait(10), "command never started"
            # The agent has returned; stop() is called exactly as dev_phase does.
            broker.stop()

        assert mock_kill.call_count == 0, "the declared timeout was truncated by the grace"
        (record,) = broker.records()
        assert record["cancelled"] is False
        assert record["exit_code"] == 0
        assert _response(broker, "r1")["success"] is True

    def test_stop_still_kills_a_command_that_outruns_its_declared_timeout(self, tmp_path):
        """The budget is the declared timeout — generous, but still terminal."""
        brief = DevVerificationCommand(
            name="check-ios", command="xcodebuild build", timeout=1, output_tail_chars=100
        )
        broker = _broker(tmp_path, commands=(brief,))
        started = threading.Event()
        release = threading.Event()
        killed: list[object] = []

        def wedged_command(cmd, cwd, **kwargs):
            kwargs["on_process_start"](object())
            started.set()
            assert release.wait(10), "command was never terminated"
            return (False, "partial build output", -9, False)

        def fake_kill(proc):
            killed.append(proc)
            release.set()
            return True

        with (
            patch_gate_shell(side_effect=wedged_command),
            patch("theforge.coordinator.util._kill_process_group", side_effect=fake_kill),
            patch("theforge.coordinator.dev_verification._SHUTDOWN_GRACE_SECONDS", 0.05),
            patch("theforge.coordinator.dev_verification._TIMEOUT_SETTLE_SECONDS", 0.1),
        ):
            broker.start()
            _request(broker, "r1", {"command": "check-ios"})
            assert started.wait(10), "command never started"
            broker.stop()

        assert killed, "a command past its declared budget must not survive shutdown"
        (record,) = broker.records()
        assert record["cancelled"] is True
        assert record["refusal_reason"] == "cancelled_at_iteration_end"

    def test_stop_falls_back_to_the_fixed_grace_with_no_command_in_flight(self, tmp_path):
        broker = _broker(tmp_path)

        with patch("theforge.coordinator.dev_verification._SHUTDOWN_GRACE_SECONDS", 5.0):
            broker.start()
            elapsed_start = time.monotonic()
            broker.stop()
            elapsed = time.monotonic() - elapsed_start

        # Nothing was running, so the idle thread exits at once rather than
        # burning the fallback grace.
        assert elapsed < 4.0
        assert broker.records() == []

    def test_an_explicit_stop_timeout_overrides_the_derived_budget(self, tmp_path):
        """Callers that pass a budget get it, declared timeout notwithstanding."""
        broker = _broker(tmp_path)  # VERIFY_WATCH declares 900s
        started = threading.Event()
        release = threading.Event()
        killed: list[object] = []

        def wedged_command(cmd, cwd, **kwargs):
            kwargs["on_process_start"](object())
            started.set()
            assert release.wait(10), "command was never terminated"
            return (False, "", -9, False)

        def fake_kill(proc):
            killed.append(proc)
            release.set()
            return True

        with (
            patch_gate_shell(side_effect=wedged_command),
            patch("theforge.coordinator.util._kill_process_group", side_effect=fake_kill),
        ):
            broker.start()
            _request(broker, "r1", {"command": "verify-watch"})
            assert started.wait(10), "command never started"
            broker.stop(timeout=0.2)

        assert killed, "an explicit stop timeout must bound the wait"

    def test_the_derived_budget_shrinks_as_the_command_burns_its_own_timeout(self, tmp_path):
        """The wait is the *remaining* budget, not the declared value restarted."""
        import theforge.coordinator.dev_verification as dv

        declared = DevVerificationCommand(
            name="check-ios", command="xcodebuild build", timeout=100, output_tail_chars=100
        )
        broker = _broker(tmp_path, commands=(declared,))
        now = time.monotonic()
        broker._active = dv._ActiveCommand("r1", declared, None, now - 60.0)

        with (
            patch.object(dv, "_SHUTDOWN_GRACE_SECONDS", 30.0),
            patch.object(dv, "_TIMEOUT_SETTLE_SECONDS", 5.0),
        ):
            budget = broker._shutdown_budget()

        assert 40.0 <= budget <= 46.0, budget

    def test_the_fixed_grace_is_a_floor_for_a_command_already_past_its_budget(self, tmp_path):
        import theforge.coordinator.dev_verification as dv

        declared = DevVerificationCommand(
            name="check-ios", command="xcodebuild build", timeout=10, output_tail_chars=100
        )
        broker = _broker(tmp_path, commands=(declared,))
        broker._active = dv._ActiveCommand("r1", declared, None, time.monotonic() - 500.0)

        with (
            patch.object(dv, "_SHUTDOWN_GRACE_SECONDS", 30.0),
            patch.object(dv, "_TIMEOUT_SETTLE_SECONDS", 5.0),
        ):
            # Its own timeout handling is due to fire; shutdown gives that room
            # to land rather than pre-empting the honest timed_out record.
            assert broker._shutdown_budget() == 30.0


class TestAuditRecords:
    def test_records_carry_the_outcome_without_duplicating_output(self, tmp_path):
        broker = _broker(tmp_path, max_requests=2)
        _request(broker, "good", {"command": "verify-watch"})
        _request(broker, "bad", {"command": "undeclared"})

        with patch_gate_shell(
            return_value=(False, "y" * 5000, 65, False),
        ):
            broker.poll_once()

        records = {record["request_id"]: record for record in broker.records()}
        assert records["good"] == {
            "iteration": 1,
            "request_id": "good",
            "command_name": "verify-watch",
            "accepted": True,
            "refusal_reason": None,
            "exit_code": 65,
            "timed_out": False,
            "duration_s": records["good"]["duration_s"],
            "output_truncated": True,
            "trace_path": ".forge/traces/verify-iter1-good.txt",
            "cancelled": False,
        }
        assert records["bad"]["accepted"] is False
        assert records["bad"]["refusal_reason"] == "unknown_command"
        assert records["bad"]["trace_path"] is None
        # The output body itself lives in the trace, not the audit record.
        assert all("output_tail" not in record for record in broker.records())


class TestConfigValidation:
    def _load(self, tmp_path: Path, validation: dict):
        (tmp_path / "forge.yaml").write_text(
            yaml.dump(
                {
                    "project": "p",
                    "models": ["claude/sonnet"],
                    "validation": {"gate_command": "make gate", **validation},
                }
            ),
            encoding="utf-8",
        )
        with (
            patch("theforge.config.load.check_agent_auth", return_value=(True, "")),
            patch("importlib.import_module"),
        ):
            return load_config(tmp_path / "forge.yaml")

    def test_absent_declaration_offers_no_capability(self, tmp_path):
        config = self._load(tmp_path, {})
        assert config.validation.dev_verification_commands == ()

    def test_short_form_declares_a_whole_command(self, tmp_path):
        config = self._load(
            tmp_path, {"dev_verification_commands": {"verify-watch": "xcodebuild test"}}
        )
        (entry,) = config.validation.dev_verification_commands
        assert entry.name == "verify-watch"
        assert entry.command == "xcodebuild test"
        assert entry.timeout == 600
        assert config.validation.dev_verification_command("verify-watch") is entry
        assert config.validation.dev_verification_command("nope") is None

    def test_full_form_carries_bounded_limits(self, tmp_path):
        config = self._load(
            tmp_path,
            {
                "dev_verification_commands": {
                    "verify-watch": {
                        "command": "xcodebuild test",
                        "timeout": 1200,
                        "output_tail_chars": 8000,
                    }
                },
                "dev_verification_max_requests": 3,
            },
        )
        (entry,) = config.validation.dev_verification_commands
        assert (entry.timeout, entry.output_tail_chars) == (1200, 8000)
        assert config.validation.dev_verification_max_requests == 3

    @pytest.mark.parametrize(
        ("validation", "message"),
        [
            ({"dev_verification_commands": ["verify"]}, "must be a mapping"),
            ({"dev_verification_commands": {"": "x"}}, "invalid command name"),
            ({"dev_verification_commands": {"../escape": "x"}}, "invalid command name"),
            ({"dev_verification_commands": {"v": ""}}, "non-empty shell command"),
            ({"dev_verification_commands": {"v": {"timeout": 5}}}, "non-empty shell command"),
            ({"dev_verification_commands": {"v": {"command": "x", "shell": True}}}, "unknown"),
            ({"dev_verification_commands": {"v": {"command": "x", "timeout": 0}}}, "positive"),
            ({"dev_verification_commands": {"v": {"command": "x", "timeout": "y"}}}, "positive"),
            ({"dev_verification_max_requests": 0}, "must be positive"),
            ({"dev_verification_max_requests": "many"}, "integer request count"),
        ],
    )
    def test_malformed_declarations_fail_closed_at_load_time(self, tmp_path, validation, message):
        with pytest.raises(ValueError, match=message):
            self._load(tmp_path, validation)


class TestPromptSection:
    def test_section_is_absent_when_nothing_is_declared(self):
        assert (
            render_verification_section(
                commands=(), request_dir=None, response_dir=None, max_requests=0
            )
            == ""
        )

    def test_section_names_the_commands_and_the_protocol(self):
        section = render_verification_section(
            commands=(("verify-watch", "xcodebuild -scheme Watch test"),),
            request_dir="/w/.forge/verify/iter-1/requests",
            response_dir="/w/.forge/verify/iter-1/responses",
            max_requests=4,
        )
        assert "verify-watch" in section
        assert "xcodebuild -scheme Watch test" in section
        assert "/w/.forge/verify/iter-1/requests/<request-id>.json" in section
        assert "/w/.forge/verify/iter-1/responses/<request-id>.json" in section
        assert '{"command": "<name>"}' in section
        assert "at most 4 request(s)" in section
        # The agent is told it asks by name, and that the gate stays coordinator-owned.
        assert "by name" in section
        assert "coordinator still runs authoritatively" in section
        # The agent is told the coordinator kills an in-flight command when the
        # iteration ends, so it knows not to yield its turn while waiting (#2077).
        assert "killed" in section
        assert "does not survive ending it" in section
