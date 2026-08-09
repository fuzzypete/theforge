"""Coordinator-owned broker for project-declared dev verification commands (ADR-0007).

A dev agent runs inside a host sandbox (``sandbox-exec`` / ``bwrap``). Seatbelt
confinement is a property of the *process tree* — every descendant inherits it
and no profile can un-confine a child — so a toolchain that itself invokes
``sandbox-exec`` (SwiftPM compiling package manifests) cannot run inside the dev
agent's process tree at all. That is issue #2050: the agent could not execute
the very commands that would judge its work, and iterated blind against the gate.

ADR-0007 settles the shape of the fix: **the project declares whole named
commands, and the agent's granted capability is the request, never the
execution.** This module is the coordinator side of that. It watches a
per-iteration request directory inside the worktree while the dev agent runs,
accepts only names the project declared in ``forge.yaml``, and executes the
configured command through the same unsandboxed shell primitive the gate already
uses (``coordinator.util._run_shell_detailed``).

Three properties are load-bearing and deliberately live here rather than in
runner policy:

* **The agent controls only a name.** Never argv, never shell text. The command
  string comes from configuration that the agent cannot reach; an unknown name
  is refused, not executed.
* **The budget is fail-closed and per-iteration.** Every request handled counts,
  accepted *or* refused, so a loop of malformed requests cannot buy unbounded
  coordinator execution. The broker's lifetime spans the whole dev iteration
  including transport retries, so the budget does not reset when ``run_agent``
  is re-attempted.
* **Every invocation is auditable by construction.** Each request produces a
  record with its outcome and a trace path to the full output, because the
  coordinator initiated it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from theforge.config.types import DevVerificationCommand
from theforge.process_group import ProcessTeardown
from theforge.traces import write_trace

from . import util as _cu

# The request id is agent-chosen and becomes a filename, so it is constrained to
# a flat, traversal-free token. Anything else is refused without touching disk.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_POLL_INTERVAL_S = 0.25
# A request file is written by another process, so the broker can observe it
# mid-write. Rather than refusing a truncated read outright, retry for roughly
# this many polls before treating the file as genuinely malformed. Agents are
# told to write atomically (tmp + rename); this is the belt to that suspenders.
_MALFORMED_RETRY_POLLS = 12
# Fallback shutdown budget, used only when shutdown cannot derive a budget from
# the in-flight command itself (nothing running, or a command declared with no
# meaningful timeout). It is a floor, never a cap: a command's declared
# ``timeout`` is a promise about how long that work is permitted to take, and a
# fixed constant that truncates it makes every configured value describe
# something that cannot happen (#2078).
_SHUTDOWN_GRACE_SECONDS = 30.0
# Slack added to a command's own remaining budget when waiting it out at
# shutdown, so ``_run_shell_detailed``'s timeout handling — kill the process
# group, drain output, return ``timed_out=True`` — gets to land and produce the
# honest record rather than being pre-empted a moment before it fires.
_TIMEOUT_SETTLE_SECONDS = 5.0
# After the kill, how long to wait for the serving thread to finish recording
# the terminated command's outcome.
_CANCEL_JOIN_SECONDS = 15.0

# Sentinel: the request file could not be parsed *yet* and should be retried on
# a later poll rather than refused. Distinct from ``None`` (genuinely malformed).
_RETRY_LATER = object()

REQUESTS_SUBDIR = "requests"
RESPONSES_SUBDIR = "responses"


@dataclass
class VerificationRequestRecord:
    """Audit record for one handled verification request."""

    iteration: int
    request_id: str
    command_name: str | None
    accepted: bool
    refusal_reason: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float = 0.0
    output_truncated: bool = False
    trace_path: str | None = None
    # True when the coordinator killed this command because the dev iteration
    # ended while it was still running. Distinct from ``timed_out`` (the command
    # exceeded its own declared budget) and from a plain non-zero exit: a
    # cancelled command proves nothing about the code, and a reader that cannot
    # tell the two apart would read a kill as a real verification failure.
    cancelled: bool = False
    # Set when the command left processes running that teardown had to kill
    # (#2309). A declared verification command is usually a build or a test run,
    # which is the shape that leaves workers behind — and a killed worker
    # produces no artifact, no exit code and no failure of its own, so this
    # record is the only place the run can say it happened.
    process_teardown: ProcessTeardown | None = None

    def audit_payload(self) -> dict:
        """Return the JSON-safe dict recorded in dev-iteration telemetry.

        The command *output* is deliberately absent: it lives in the trace file
        named by ``trace_path``, under the same tail/trace handling the gate
        already uses, so the audit record does not become a second copy of
        whatever a build printed.
        """
        return {
            "iteration": self.iteration,
            "request_id": self.request_id,
            "command_name": self.command_name,
            "accepted": self.accepted,
            "refusal_reason": self.refusal_reason,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 2),
            "output_truncated": self.output_truncated,
            "trace_path": self.trace_path,
            "cancelled": self.cancelled,
            # Written only when it happened, like the agent entries': a command
            # that left nothing behind should say nothing rather than carry a
            # null on every record.
            **(
                {"process_teardown": self.process_teardown.to_audit_dict()}
                if self.process_teardown is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class _ActiveCommand:
    """The declared command currently executing, as shutdown needs to see it.

    ``started`` is a ``time.monotonic`` stamp taken just before the subprocess
    is launched, so shutdown can compute how much of the command's declared
    budget is left rather than applying a budget of its own.
    """

    request_id: str
    declared: DevVerificationCommand
    proc: object | None
    started: float

    def remaining_budget(self, now: float) -> float:
        """Return seconds left of this command's declared timeout, floored at 0."""
        if self.declared.timeout is None or self.declared.timeout <= 0:
            return 0.0
        return max(0.0, float(self.declared.timeout) - (now - self.started))


@dataclass
class DevVerificationBroker:
    """Serve declared verification requests for one dev iteration.

    Construct it before prompt building (the prompt must name the request
    directory), :meth:`start` it immediately before the agent runs, and
    :meth:`stop` it in a ``finally`` once the agent has returned. Requests are
    served on a single background thread, so declared commands never run
    concurrently with each other.
    """

    workspace_path: Path
    commands: tuple[DevVerificationCommand, ...]
    iteration: int
    max_requests: int = 10
    expected_python: str | None = None

    _records: list[VerificationRequestRecord] = field(default_factory=list, init=False)
    _seen: set[str] = field(default_factory=set, init=False)
    _malformed_polls: dict[str, int] = field(default_factory=dict, init=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    # The command currently executing, if any, guarded by ``_active_lock``.
    # ``stop`` reads this from the coordinator thread both to size its wait
    # against the command's own declared budget and to kill a command that
    # outlived that budget; ``_execute`` reads ``_cancelled_ids`` afterwards to
    # record the kill honestly.
    _active: _ActiveCommand | None = field(default=None, init=False)
    _active_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _cancelled_ids: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.request_dir.mkdir(parents=True, exist_ok=True)
        self.response_dir.mkdir(parents=True, exist_ok=True)

    # ── Paths ────────────────────────────────────────────────────────────
    @property
    def channel_dir(self) -> Path:
        return self.workspace_path / ".forge" / "verify" / f"iter-{self.iteration}"

    @property
    def request_dir(self) -> Path:
        return self.channel_dir / REQUESTS_SUBDIR

    @property
    def response_dir(self) -> Path:
        return self.channel_dir / RESPONSES_SUBDIR

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.commands)

    def records(self) -> list[dict]:
        """Return audit payloads for every request handled so far."""
        return [record.audit_payload() for record in self._records]

    # ── Lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        """Begin serving requests on a background thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name=f"forge-dev-verify-iter{self.iteration}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop serving, and guarantee no declared command outlives its own budget.

        The agent can exit while a command it asked for is still running.
        Dropping that command silently is not an option: the request would leave
        no audit record at all, which is precisely the invisibility this
        capability exists to remove. Neither is letting it run unbounded — an
        unconfined build still writing to the worktree would race the
        coordinator's own authoritative gate, which then measures a tree the
        build was still mutating.

        So shutdown is bounded and terminal, but the bound is the command's
        *own* declared ``timeout``, not a constant of this module's choosing.
        A command declared at 1800s that has run 40s gets the remaining ~1760s
        to finish; only past that is its process group killed and the real
        outcome of the kill recorded by the serving thread. A fixed grace here
        would make every declared timeout unreachable and every configured value
        describe something that cannot occur (#2078).

        ``timeout``, when passed, overrides that derivation outright — callers
        that know the budget they want (tests, mostly) still get it.
        """
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.join(timeout=self._shutdown_budget() if timeout is None else timeout)
        if not thread.is_alive():
            return
        cancelled = self._cancel_active()
        thread.join(timeout=_CANCEL_JOIN_SECONDS)
        if cancelled is not None:
            self._backfill_cancelled_record(cancelled)

    def _shutdown_budget(self) -> float:
        """Return how long shutdown waits for the in-flight command, in seconds.

        ``_SHUTDOWN_GRACE_SECONDS`` is a floor, not a cap: it covers the case
        where there is nothing running (or a command with no meaningful declared
        budget) and a request could still be mid-flight, while a command that
        declared a longer budget keeps all of it.
        """
        with self._active_lock:
            active = self._active
        if active is None:
            return _SHUTDOWN_GRACE_SECONDS
        remaining = active.remaining_budget(time.monotonic())
        if remaining <= 0:
            # Its own timeout is already due; ``_run_shell_detailed`` is about to
            # fire it, so allow that to land rather than pre-empting the honest
            # ``timed_out=True`` record with a cancellation.
            return _SHUTDOWN_GRACE_SECONDS
        return max(_SHUTDOWN_GRACE_SECONDS, remaining + _TIMEOUT_SETTLE_SECONDS)

    def _cancel_active(self) -> tuple[str, DevVerificationCommand] | None:
        """Kill the in-flight declared command's process group, if any.

        Returns the ``(request_id, command)`` that was cancelled so the caller
        can confirm it was recorded. Reuses the coordinator's own group-kill
        helper — the same one the gate uses on timeout — so grandchildren of the
        build (compiler jobs, simulators) go with it rather than being orphaned.
        """
        with self._active_lock:
            active = self._active
            if active is None:
                return None
            request_id, declared, proc = active.request_id, active.declared, active.proc
            self._cancelled_ids.add(request_id)
        _cu._log(
            f"  ⚠ DEV   verification {declared.name!r} (request {request_id}) was still "
            f"running when its declared budget of {declared.timeout}s ran out after the "
            "dev iteration ended — terminating it"
        )
        if proc is not None:
            try:
                _cu._kill_process_group(proc)
            except Exception as exc:  # noqa: BLE001  # teardown must not raise into DEV
                _cu._log(f"  ⚠ DEV   could not terminate verification {declared.name!r}: {exc}")
        return request_id, declared

    def _backfill_cancelled_record(self, cancelled: tuple[str, DevVerificationCommand]) -> None:
        """Record a cancelled command the serving thread never got to record.

        The kill normally unblocks ``_execute``, which writes the real terminal
        record. This covers the case where it did not: the audit trail must name
        every unconfined execution the coordinator started, including one whose
        ending it could not observe.
        """
        request_id, declared = cancelled
        if any(record.request_id == request_id for record in self._records):
            return
        self._record(
            VerificationRequestRecord(
                iteration=self.iteration,
                request_id=request_id,
                command_name=declared.name,
                accepted=True,
                refusal_reason="cancelled_at_iteration_end",
                cancelled=True,
            ),
            body={
                "success": False,
                "detail": (
                    "the dev iteration ended while this command was still running; "
                    "it was terminated and its outcome is unknown"
                ),
            },
        )

    # ── Serving ──────────────────────────────────────────────────────────
    def _serve(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001  # a broker fault must not kill DEV
                _cu._log(f"  ⚠ DEV   verification broker error: {exc}")
            self._stop_event.wait(_POLL_INTERVAL_S)
        # Drain whatever landed in the last poll interval so a request written
        # just before the agent exited still gets an answer on disk and an audit
        # record — but refuse rather than execute. Starting a minutes-long
        # unconfined build as the iteration ends is the very thing shutdown
        # exists to prevent, and no one is left to read the result.
        try:
            self.poll_once()
        except Exception as exc:  # noqa: BLE001
            _cu._log(f"  ⚠ DEV   verification broker error: {exc}")

    @property
    def _shutting_down(self) -> bool:
        return self._stop_event.is_set()

    def poll_once(self) -> None:
        """Handle every unseen request file currently in the request directory."""
        try:
            entries = sorted(self.request_dir.glob("*.json"))
        except OSError:
            return
        for path in entries:
            request_id = path.stem
            if request_id in self._seen:
                continue
            payload = self._read_request(path, request_id)
            if payload is _RETRY_LATER:
                continue
            self._seen.add(request_id)
            self._handle(request_id, payload)

    def _read_request(self, path: Path, request_id: str) -> object:
        """Return the parsed request body, ``_RETRY_LATER``, or ``None`` if malformed."""
        try:
            raw = path.read_text(encoding="utf-8")
            return json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError):
            polls = self._malformed_polls.get(request_id, 0) + 1
            self._malformed_polls[request_id] = polls
            if polls < _MALFORMED_RETRY_POLLS:
                # Very likely a partially-written file; give the writer time.
                return _RETRY_LATER
            return None

    def _handle(self, request_id: str, payload: object) -> None:
        if not _REQUEST_ID_RE.match(request_id):
            # Cannot write a response keyed to this id, so only record it.
            self._record(
                VerificationRequestRecord(
                    iteration=self.iteration,
                    request_id=request_id[:64],
                    command_name=None,
                    accepted=False,
                    refusal_reason="invalid_request_id",
                ),
                response=False,
            )
            return
        if len(self._records) >= self.max_requests:
            self._refuse(
                request_id,
                None,
                "request_limit_exceeded",
                detail=(
                    f"this dev iteration has already used its budget of "
                    f"{self.max_requests} verification request(s)"
                ),
            )
            return
        if payload is None:
            self._refuse(
                request_id, None, "malformed_request", detail="request file is not valid JSON"
            )
            return
        if not isinstance(payload, dict):
            self._refuse(
                request_id,
                None,
                "malformed_request",
                detail='request body must be a JSON object like {"command": "<name>"}',
            )
            return
        name = payload.get("command")
        if not isinstance(name, str) or not name:
            self._refuse(
                request_id,
                None,
                "malformed_request",
                detail='request body must set "command" to a declared command name',
            )
            return
        declared = next((entry for entry in self.commands if entry.name == name), None)
        if declared is None:
            self._refuse(
                request_id,
                name,
                "unknown_command",
                detail=(
                    f"{name!r} is not declared by this project; declared commands are: "
                    + (", ".join(self.command_names) or "(none)")
                ),
            )
            return
        if self._shutting_down:
            self._refuse(
                request_id,
                name,
                "iteration_ended",
                detail=(
                    "the dev iteration ended before this request was served; the command "
                    "was not started"
                ),
            )
            return
        self._execute(request_id, declared)

    def _execute(self, request_id: str, declared: DevVerificationCommand) -> None:
        _cu._log(
            f"  ▶ DEV   verification request {request_id}: running declared "
            f"command {declared.name!r} outside the sandbox"
        )
        started = time.monotonic()

        def _publish(proc: object) -> None:
            # Publish the live process the moment it exists so ``stop`` can kill
            # its group. Registered *before* the run rather than after, because
            # the whole point is to reach a command that has not finished.
            with self._active_lock:
                self._active = _ActiveCommand(request_id, declared, proc, started)

        # Published before the run too, so a ``stop`` racing the launch sizes its
        # wait against this command's declared budget rather than seeing nothing
        # in flight and falling back to the fixed grace.
        with self._active_lock:
            self._active = _ActiveCommand(request_id, declared, None, started)
        teardowns: list[ProcessTeardown] = []
        try:
            ok, output, exit_code, timed_out = _cu._run_shell_detailed(
                declared.command,
                self.workspace_path,
                timeout=declared.timeout,
                expected_python=self.expected_python,
                on_process_start=_publish,
                teardown_out=teardowns,
            )
        finally:
            with self._active_lock:
                self._active = None
                cancelled = request_id in self._cancelled_ids
        duration = time.monotonic() - started
        output = output or ""
        tail = output[-declared.output_tail_chars :]
        trace_rel = f".forge/traces/verify-iter{self.iteration}-{request_id}.txt"
        try:
            write_trace(self.workspace_path / trace_rel, output)
            trace_path: str | None = trace_rel
        except Exception:  # noqa: BLE001  # a missing trace must not fail the request
            trace_path = None
        record = VerificationRequestRecord(
            iteration=self.iteration,
            request_id=request_id,
            command_name=declared.name,
            accepted=True,
            # A cancelled command's non-zero exit is an artifact of the kill, not
            # a verdict on the code, so it is labelled as such rather than left
            # to read as an ordinary verification failure.
            refusal_reason="cancelled_at_iteration_end" if cancelled else None,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_s=duration,
            output_truncated=len(output) > len(tail),
            trace_path=trace_path,
            cancelled=cancelled,
            process_teardown=teardowns[0] if teardowns else None,
        )
        self._record(
            record,
            body={
                "success": bool(ok) and not timed_out and not cancelled,
                # The whole command as declared in forge.yaml, echoed so the
                # agent can read what actually ran. ``command`` stays the
                # requested *name* — the only token the agent controls.
                "resolved_command": declared.command,
                "output_tail": tail,
            },
        )
        if cancelled:
            _cu._log(
                f"  ⚠ DEV   verification {declared.name!r} terminated after "
                f"{duration:.1f}s — the dev iteration ended while it was running"
            )
        else:
            _cu._log(
                f"  {'✓' if ok and not timed_out else '✗'} DEV   verification "
                f"{declared.name!r} exit={exit_code} timed_out={timed_out} ({duration:.1f}s)"
            )

    def _refuse(self, request_id: str, name: str | None, reason: str, *, detail: str) -> None:
        _cu._log(f"  ⚠ DEV   verification request {request_id} refused ({reason}): {detail}")
        self._record(
            VerificationRequestRecord(
                iteration=self.iteration,
                request_id=request_id,
                command_name=name,
                accepted=False,
                refusal_reason=reason,
            ),
            body={"success": False, "detail": detail},
        )

    def _record(
        self,
        record: VerificationRequestRecord,
        *,
        body: dict | None = None,
        response: bool = True,
    ) -> None:
        self._records.append(record)
        if response:
            self._write_response(record, body or {})

    def _write_response(self, record: VerificationRequestRecord, body: dict) -> None:
        """Write the response artifact atomically.

        The agent polls for the final path, so it must never observe a partially
        written response and read it as a real result. Written to a temp file in
        the same directory and ``os.replace``d into place.
        """
        payload = {
            "request_id": record.request_id,
            "command": record.command_name,
            "accepted": record.accepted,
            "refusal_reason": record.refusal_reason,
            "exit_code": record.exit_code,
            "timed_out": record.timed_out,
            "duration_s": round(record.duration_s, 2),
            "output_truncated": record.output_truncated,
            "trace_path": record.trace_path,
            "cancelled": record.cancelled,
            **body,
        }
        final = self.response_dir / f"{record.request_id}.json"
        tmp = self.response_dir / f".{record.request_id}.json.tmp"
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, final)
        except OSError as exc:
            _cu._log(f"  ⚠ DEV   failed to write verification response {final}: {exc}")
