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
        }


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

    def stop(self, timeout: float = 30.0) -> None:
        """Stop serving and join the thread.

        A declared command may still be executing when the agent exits; the join
        timeout bounds how long the coordinator waits for it rather than
        blocking the run indefinitely on a build the agent no longer needs.
        """
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    # ── Serving ──────────────────────────────────────────────────────────
    def _serve(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001  # a broker fault must not kill DEV
                _cu._log(f"  ⚠ DEV   verification broker error: {exc}")
            self._stop_event.wait(_POLL_INTERVAL_S)
        # Drain whatever landed in the last poll interval so a request written
        # just before the agent exited still gets an answer on disk.
        try:
            self.poll_once()
        except Exception as exc:  # noqa: BLE001
            _cu._log(f"  ⚠ DEV   verification broker error: {exc}")

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
        self._execute(request_id, declared)

    def _execute(self, request_id: str, declared: DevVerificationCommand) -> None:
        _cu._log(
            f"  ▶ DEV   verification request {request_id}: running declared "
            f"command {declared.name!r} outside the sandbox"
        )
        started = time.monotonic()
        ok, output, exit_code, timed_out = _cu._run_shell_detailed(
            declared.command,
            self.workspace_path,
            timeout=declared.timeout,
            expected_python=self.expected_python,
        )
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
            exit_code=exit_code,
            timed_out=timed_out,
            duration_s=duration,
            output_truncated=len(output) > len(tail),
            trace_path=trace_path,
        )
        self._record(
            record,
            body={
                "success": bool(ok) and not timed_out,
                # The whole command as declared in forge.yaml, echoed so the
                # agent can read what actually ran. ``command`` stays the
                # requested *name* — the only token the agent controls.
                "resolved_command": declared.command,
                "output_tail": tail,
            },
        )
        _cu._log(
            f"  {'✓' if ok and not timed_out else '✗'} DEV   verification {declared.name!r} "
            f"exit={exit_code} timed_out={timed_out} ({duration:.1f}s)"
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
            **body,
        }
        final = self.response_dir / f"{record.request_id}.json"
        tmp = self.response_dir / f".{record.request_id}.json.tmp"
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, final)
        except OSError as exc:
            _cu._log(f"  ⚠ DEV   failed to write verification response {final}: {exc}")
