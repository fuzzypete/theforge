"""Coordinator utilities: logging, run-id generation, and shell execution.

Extracted from coord_state.py so that coord_state.py can remain stdlib-only
(dataclasses/enums only, no project imports at runtime).
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from theforge.log_level import LogLevel
from theforge.log_util import _log_line
from theforge.process_group import (
    KILL_GRACE_SECONDS,
    ProcessTeardown,
    descendant_tracker,
    is_killable_pgid,
    open_process_lease,
    register_agent_group,
    release_group_record,
)
from theforge.workspace_env import build_workspace_env

# Stable reference captured at import time so test patches that replace the
# subprocess module attribute (e.g. patch("validate_phase.subprocess.run"))
# do not accidentally intercept worktree eval subprocess calls.
_subprocess_run = subprocess.run

# Absolute path to the subprocess eval entry point — invoked by path so the
# coordinator's own version runs regardless of what the installed package provides.
_SUBPROCESS_EVAL = Path(__file__).parent / "_subprocess_eval.py"
_CHECKOUT_SRC = Path(__file__).resolve().parents[2]

# Adaptive timeout fallback uses the same default headroom factor across
# surfaces when forge.yaml does not provide an explicit complexity-tier
# override. Keep the legacy medium/large names as public aliases for tests and
# existing call sites.
DEFAULT_TIMEOUT_HEADROOM_FACTOR = 1.5
MEDIUM_HEADROOM_FACTOR = DEFAULT_TIMEOUT_HEADROOM_FACTOR
LARGE_HEADROOM_FACTOR = DEFAULT_TIMEOUT_HEADROOM_FACTOR


# ── Live-state complexity payload ─────────────────────────────────────


def live_complexity_fields(complexity: object, complexity_score: "int | None") -> dict:
    """Build the complexity portion of a live-state ``state_update_fn`` payload.

    Always emits the band under ``complexity``. Emits ``complexity_score`` only
    when a numeric score is known. This centralizes the hazard rule: the sprint
    state writer (``SprintStoryState.transition``) treats an *absent*
    ``complexity_score`` key as "preserve the established score" but an explicit
    ``complexity_score: None`` as "clear it". A ``None`` score means the score
    has not been computed yet (e.g. the first preflight payload, fired before
    the parse step), so we omit the key rather than wipe a score an earlier
    phase already recorded. Once a numeric score exists, every phase's payload
    carries it so the live display's numeric form holds uniformly.
    """
    fields: dict = {"complexity": complexity}
    if complexity_score is not None:
        fields["complexity_score"] = complexity_score
    return fields


# ── Log level ─────────────────────────────────────────────────────────

_LOG_LEVEL: LogLevel = LogLevel.PROGRESS


def set_log_level(level: LogLevel) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level


# ── Logging ──────────────────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    """Format duration as '2h 14m 3s', '14m 3s', or '47s'."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _log(msg: str) -> None:
    """Print coordinator status to stderr (always shown)."""
    # The worker-slug prefix (parallel attribution) is applied centrally by
    # ``_log_line``; do not prepend it here or it would double-tag.
    _log_line("[forge]", msg)


def _log_verbose(msg: str) -> None:
    """Print coordinator detail to stderr (verbose mode only)."""
    if _LOG_LEVEL >= LogLevel.VERBOSE:
        _log_line("[forge]", msg)


def _fmt_cost(cost: float | None) -> str:
    """Format a cost value as '$1.23', or 'unknown' when cost is None."""
    return f"${cost:.2f}" if cost is not None else "unknown"


def sum_costs(values: Iterable[float | None]) -> float | None:
    """Sum optional costs, returning ``None`` if *any* contributor is unmeasured.

    ``None`` means "the transport could not measure this spend", which is a
    different statement from ``0.0`` ("this was free"). Coercing the former to
    the latter reports unpriced work as free, so a single unmeasured addend
    makes the whole aggregate cost-unknown. An empty iterable is genuinely no
    spend, so ``0.0``.
    """
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _fmt_cost_total(total: float | None, subtotal: float | None = None) -> str:
    """Render an aggregate cost for an operator.

    A fully measured aggregate renders as ``$1.23``. An aggregate with any
    unmeasured component renders as ``unknown`` — never as a dollar figure,
    which would present a partial total as a complete one. When a measured
    subtotal is available it is shown as an explicit lower bound
    (``unknown (>= $0.99 measured)``) so the operator sees both that money was
    spent and that the figure is incomplete.
    """
    if total is not None:
        return f"${total:.2f}"
    if subtotal:
        return f"unknown (>= ${subtotal:.2f} measured)"
    return "unknown"


def _round_cost(value: float | None, digits: int = 6) -> float | None:
    """Round an optional cost, preserving an unmeasured ``None`` as ``None``."""
    return round(value, digits) if value is not None else None


def _log_phase(phase: object, detail: str = "") -> None:
    suffix = f"   {detail}" if detail else ""
    _log(f"▸ {phase.name}{suffix}")  # type: ignore[attr-defined]


# ── Run ID ───────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Return a short random hex run ID (12 chars)."""
    return secrets.token_hex(6)


# ── Shell helper ─────────────────────────────────────────────────────


def resolve_timeout(
    base: int,
    medium: int | None,
    large: int | None,
    complexity: str | None,
    complexity_score: int | None = None,
) -> int:
    """Return the appropriate timeout for the given preflight complexity.

    Converted routing sites prefer the numeric score when present so stories in
    the same legacy band can still diverge. Current policy: 8-10 uses the large
    override, 6-7 uses the medium override, and lower scores fall back to base.
    When no score is available, legacy band-based behavior is preserved.
    """
    return resolve_timeout_with_active(base, medium, large, complexity, complexity_score)[0]


def resolve_timeout_with_active(
    base: int,
    medium: int | None,
    large: int | None,
    complexity: str | None,
    complexity_score: int | None = None,
) -> tuple[int, bool]:
    """Return ``(timeout, override_active)`` for the given complexity inputs.

    Score-native routing preserves tier isolation: large-score stories use the
    large tier and medium-score stories use the medium tier. When an explicit
    tier override is absent, the timeout is derived from the base timeout using
    the corresponding headroom factor instead of silently falling back to base.
    Large stories must not cascade into the medium tier when the large tier is
    unset.
    """

    def _derived_timeout(factor: float) -> int:
        return round(base * factor)

    if complexity_score is not None:
        if complexity_score >= 8:
            if large is not None:
                return large, True
            return _derived_timeout(LARGE_HEADROOM_FACTOR), True
        if complexity_score >= 6:
            if medium is not None:
                return medium, True
            return _derived_timeout(MEDIUM_HEADROOM_FACTOR), True
        return base, False
    if complexity == "large":
        if large is not None:
            return large, True
        return _derived_timeout(LARGE_HEADROOM_FACTOR), True
    if complexity == "medium":
        if medium is not None:
            return medium, True
        return _derived_timeout(MEDIUM_HEADROOM_FACTOR), True
    return base, False


#: Share of an enclosing story ceiling that development invocations may claim.
#: The remainder funds validate, review, the landing path, and the gaps between
#: them — all of which are charged to the same wall clock.
DEV_INVOCATION_STORY_SHARE = 0.8

#: Never cap a development invocation below this, however tight the enclosing
#: ceiling. Under this figure the invocation cannot do useful work, and a cap
#: that guarantees failure is not a budget — the shortfall is logged instead.
MIN_DEV_INVOCATION_SECONDS = 900

#: Working seconds a dev invocation must leave behind for the review and landing
#: that follow it, when clamped against the story's *remaining* budget.
DEV_INVOCATION_TAIL_RESERVE_SECONDS = 300


def cap_timeout_to_story_ceiling(
    raw_timeout_seconds: int,
    story_ceiling_seconds: float | None,
    max_invocations: int,
    *,
    share: float = DEV_INVOCATION_STORY_SHARE,
    floor_seconds: int = MIN_DEV_INVOCATION_SECONDS,
) -> tuple[int, dict]:
    """Fit a per-invocation allowance inside the story ceiling that bounds it.

    An allowance derived for one part of a story must be derived against the
    budget containing it. The complexity-derived development timeout is not: it
    comes from the dev profile's own base, so a story whose worker ceiling was
    5400s could be handed a 4950s per-invocation timeout and then be killed by
    signal on its third development cycle, mid-edit, inside a budget sized for
    barely one (#2333).

    The cap is ``share * ceiling / max_invocations`` — the allowance stays
    consistent with the ceiling at *every* invocation count the iteration path
    can reach, not only at one. ``floor_seconds`` bounds it from below: when the
    ceiling cannot fund the permitted invocations even at the floor, the floor
    wins and the audit records the shortfall rather than issuing an allowance no
    invocation could use.

    Returns ``(timeout, audit)``. ``audit`` always carries the raw timeout, the
    enclosing ceiling, the cap, and the final figure, so the values that drove
    the decision are inspectable instead of reconstructed (conventions #6).
    """
    audit: dict = {
        "raw_timeout_seconds": int(raw_timeout_seconds),
        "story_ceiling_seconds": (
            None if story_ceiling_seconds is None else round(float(story_ceiling_seconds))
        ),
        "max_invocations": int(max_invocations),
        "share": share,
        "capped": False,
    }
    if story_ceiling_seconds is None or story_ceiling_seconds <= 0:
        audit["final_timeout_seconds"] = int(raw_timeout_seconds)
        audit["rationale"] = "no enclosing story ceiling; per-invocation timeout left as derived"
        return int(raw_timeout_seconds), audit

    _invocations = max(1, int(max_invocations))
    _dev_share_seconds = int(float(story_ceiling_seconds) * share)
    cap = int(_dev_share_seconds / _invocations)
    audit["cap_seconds"] = cap
    floor_applied = cap < floor_seconds
    audit["floor_seconds"] = int(floor_seconds)
    audit["floor_applied"] = floor_applied
    audit["dev_share_seconds"] = _dev_share_seconds
    # The floor may raise a too-small cap, but never past the share of the
    # ceiling development is entitled to in total. Letting it through would hand
    # a small-ceiling story an allowance it can only use by having a single
    # cycle — the reported shape, reintroduced from the other direction.
    effective_cap = min(max(cap, int(floor_seconds)), _dev_share_seconds)
    audit["floor_capped_by_share"] = max(cap, int(floor_seconds)) > _dev_share_seconds
    final = min(int(raw_timeout_seconds), effective_cap)
    audit["capped"] = final < int(raw_timeout_seconds)
    audit["final_timeout_seconds"] = final
    if audit["capped"]:
        audit["rationale"] = (
            f"per-invocation timeout capped {raw_timeout_seconds}s → {final}s to fit "
            f"{_invocations} invocation(s) inside the {round(float(story_ceiling_seconds))}s "
            f"story ceiling (share {share})"
        )
    else:
        audit["rationale"] = (
            f"per-invocation timeout {final}s already fits {_invocations} invocation(s) "
            f"inside the {round(float(story_ceiling_seconds))}s story ceiling"
        )
    if floor_applied:
        audit["rationale"] += (
            f"; ceiling funds only {cap}s per invocation, below the {int(floor_seconds)}s floor"
        )
    if audit["floor_capped_by_share"]:
        audit["rationale"] += (
            f"; the floor itself was cut to the {_dev_share_seconds}s development share of the "
            "ceiling"
        )
    return final, audit


def clamp_timeout_to_remaining(
    timeout_seconds: int,
    remaining_seconds: float | None,
    *,
    tail_reserve_seconds: int = DEV_INVOCATION_TAIL_RESERVE_SECONDS,
    floor_seconds: int = 60,
) -> tuple[int, dict | None]:
    """Shorten an invocation so it ends inside the story deadline containing it.

    The static cap is computed once, before any cycle runs. This is the dynamic
    half: at dispatch, an invocation is given no more than the story's *own*
    remaining working time less a tail reserve for the review and landing that
    must follow. An invocation that ends on its own timeout is a recorded,
    costed, work-preserving outcome; one killed by the scheduler's deadline is a
    SIGKILL mid-edit with no measurable cost (#2333).

    ``floor_seconds`` is a minimum *useful* invocation, not a licence to outlive
    the deadline: a story with 40s of working time left funds a 40s invocation,
    never a 60s one. A floor that could exceed what remains would reintroduce
    exactly the shape this function exists to prevent — an allowance whose expiry
    necessarily falls outside the window enclosing it.

    Returns ``(timeout, audit_or_None)`` — ``None`` when nothing was clamped.
    """
    if remaining_seconds is None:
        return int(timeout_seconds), None
    _remaining = float(remaining_seconds)
    _after_reserve = max(float(floor_seconds), _remaining - tail_reserve_seconds)
    _floor_capped = _after_reserve > _remaining
    allowed = int(max(0.0, min(_after_reserve, _remaining)))
    if allowed >= int(timeout_seconds):
        return int(timeout_seconds), None
    audit = {
        "requested_timeout_seconds": int(timeout_seconds),
        "remaining_story_seconds": round(_remaining),
        "tail_reserve_seconds": int(tail_reserve_seconds),
        "floor_seconds": int(floor_seconds),
        # True when the story had less working time left than the floor, so the
        # floor itself was cut down to what remains.
        "floor_capped_by_remaining": _floor_capped,
        # True when the deadline is already spent. The invocation cannot fit at
        # all; it is granted nothing rather than a window the story cannot honour.
        "no_working_time_left": allowed <= 0,
        "granted_timeout_seconds": allowed,
        "rationale": (
            f"invocation clamped {int(timeout_seconds)}s → {allowed}s: the enclosing story "
            f"deadline has {round(_remaining)}s of working time left"
        ),
    }
    if _floor_capped:
        audit["rationale"] += (
            f"; less than the {int(floor_seconds)}s floor, so the floor was cut to what remains"
        )
    return allowed, audit


def _wait_bounded(proc: subprocess.Popen[str]) -> bool:
    """Wait for *proc*, but never longer than the teardown grace period.

    Returns True if it exited within that window.

    An unbounded wait after a best-effort kill is only correct if the kill always
    lands. It does not: a platform sandbox can refuse signal delivery outright,
    and the wait then lasts for the command's entire natural lifetime — measured
    at 300s for a ``sleep 300`` that a 0.1s timeout was meant to cut short
    (#1959). A timeout that cannot bound its own cleanup is not a timeout.
    """
    try:
        proc.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _kill_process_group(proc: subprocess.Popen[str]) -> bool:
    """Best-effort kill for a spawned shell process group, then a bounded wait.

    Returns True only when the ``killpg`` reached the whole group. False means
    the kill reached at most the direct child, so grandchildren may still be
    running and the group's reaper sidecar must be kept.

    Only ``killpg`` a pgid that denotes a real group (``> 1``); a bogus ``<= 1``
    value would broadcast SIGKILL across the whole session (see
    ``process_group.is_killable_pgid`` / #1793). Fall back to terminating just the
    direct child when the group id is unknown or unkillable.

    A refused ``killpg`` is logged rather than swallowed. It produces no error
    anywhere else and surfaces only as a teardown that takes the command's full
    natural lifetime — indistinguishable in a log from "the work was slow", which
    is why #1959 took a dedicated probe to find.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if is_killable_pgid(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError as exc:
            _log_line("[forge]", f"  ⚠ group kill of pgid={pgid} refused: {exc}")
        else:
            _wait_bounded(proc)
            return True
    # Each wait is guarded by whether the signal before it was actually
    # delivered. Waiting on a refused signal is the #1959 mistake in miniature:
    # it buys nothing and costs the grace period every time.
    try:
        proc.terminate()
    except OSError:
        pass
    else:
        if _wait_bounded(proc):
            return False
    # SIGTERM is catchable, so a shell that ignores it survives — and a survivor
    # holds the output pipes open, which is the read this function's caller then
    # blocks on (#1959). Escalate to SIGKILL rather than leave that to chance;
    # this matches process_group.terminate_process_group's escalation.
    try:
        proc.kill()
    except OSError:
        return False
    _wait_bounded(proc)
    return False


# Longest we will wait to collect a timed-out command's partial output. Small:
# the pipes hold what the command already wrote, so a read that has not returned
# by now is blocked on a writer that is still alive, not on data in flight.
_DRAIN_TIMEOUT_SECONDS = 2.0


def _drain_partial_output(
    te: subprocess.TimeoutExpired, proc: subprocess.Popen[str]
) -> tuple[str, bool]:
    """Collect what a timed-out command emitted, without blocking on its writer.

    Returns ``(output, drained)``. ``drained`` is False when the read did not
    finish, which also means the reader thread still owns the streams — see
    below.

    Reading a pipe blocks until every writer holding the other end closes it.
    After the group kill above that writer should be gone — but when the kill was
    refused (a platform sandbox denying signal delivery, #1959) it is not, and
    this read then waits out the command's entire natural lifetime. Measured: a
    ``sleep 300`` under a 0.1s timeout returned after 300s, with the wait spent
    here rather than anywhere the timeout could see it. Reading on a daemon
    thread bounds it — a timeout that cannot bound its own cleanup is not a
    timeout.

    The thread closes the streams itself once it finishes. That is not tidiness:
    a blocked ``read()`` holds the buffer lock, so a ``close()`` from this thread
    would block on that lock and reintroduce the very wait we just bounded, one
    frame further out.
    """
    chunks: list[str] = []
    for raw in (te.stdout, te.stderr):
        if raw:
            chunks.append(raw if isinstance(raw, str) else raw.decode(errors="replace"))

    tail: list[str] = []

    def _read() -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                data = stream.read()
                if isinstance(data, bytes):
                    data = data.decode(errors="replace")
                if isinstance(data, str):
                    tail.append(data)
            except Exception:  # noqa: BLE001
                pass
            try:
                stream.close()
            except Exception:  # noqa: BLE001, S110
                pass

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(_DRAIN_TIMEOUT_SECONDS)
    drained = not reader.is_alive()
    if not drained:
        _log_line(
            "[forge]",
            "  ⚠ timed-out command still holds its output pipes open "
            "(its process group survived the kill); reporting partial output",
        )
    return "".join(chunks + tail).strip(), drained


def _run_shell_detailed(
    cmd: str,
    cwd: Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
    expected_python: str | None = None,
    *,
    on_process_start: Callable[[subprocess.Popen[str]], None] | None = None,
    teardown_out: list[ProcessTeardown] | None = None,
) -> tuple[bool, str, int | None, bool]:
    """Run a shell command. Returns (success, combined output, exit_code, timed_out).

    On abnormal exit, kills the entire process group so child processes (e.g.
    pytest-xdist workers) don't outlive the shell and consume unbounded memory.

    ``expected_python`` is the patient project's pinned interpreter; when given,
    a worktree virtualenv is only put on PATH if it was built from that
    interpreter. Ignored when ``env`` is supplied, since the caller then owns
    the environment.

    ``on_process_start``, when given, is called with the live ``Popen`` as soon
    as it exists, so a caller that must be able to *cancel* this command from
    another thread has a handle on the process group. This function blocks until
    the command finishes, so without such a handle a long-running command can
    only be waited out — which is how a declared dev verification command could
    outlive the iteration that asked for it (#2050). Purely an observation hook:
    it never affects the command, and an exception from it is deliberately not
    swallowed, since a caller that cannot record the handle it asked for would
    otherwise silently lose its ability to cancel.

    ``teardown_out`` collects a `ProcessTeardown` when the command left processes
    running that had to be killed. An out-parameter because the return tuple is
    the command's *result* and this is a fact about its aftermath — and because a
    caller that does not care should not have to unpack it. Threaded on so the
    gate's own leaks reach the run record rather than only the log (#2309).
    """
    # The lease is stamped into the environment every descendant of this command
    # inherits, so teardown can reach a test worker or daemon that left the
    # process group by calling setsid — what a pgid by construction cannot
    # describe (#2309).
    leased_env, lease = open_process_lease(
        env if env is not None else build_workspace_env(cwd, expected_python=expected_python)
    )
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
            env=leased_env,
            start_new_session=True,
        )
    except Exception as e:
        return False, f"ERROR: {e}", None, False
    if on_process_start is not None:
        on_process_start(proc)
    # Record the group with the orphan reaper. ``start_new_session=True`` above
    # puts this command — a gate invocation, and everything it spawns — in its
    # own session, which is exactly why a signal to the sprint's own process
    # group never reaches it. When the sprint is killed (``forge stop``, SIGKILL)
    # the cleanup below never runs, and without this sidecar there is no record
    # left for ``reap_orphan_agents`` to kill the tree by: an xcodebuild survived
    # a stop that reported success (#2013).
    # ``start_new_session=True`` makes the child the leader of its own session
    # and group, so its pgid *is* its pid — no ``getpgid`` round trip needed, and
    # none possible once the child has already exited. The guard keeps a pid that
    # cannot denote a real group out of the registry (#1793).
    pgid: int | None = proc.pid if is_killable_pgid(proc.pid) else None
    if pgid is not None:
        register_agent_group(pgid, sandbox_dir=str(cwd), lease=lease)
    # Watches what the gate command starts. `make gate` runs the project's test
    # runner, which is exactly the shape that spawns long-lived workers, and a
    # worker that leaves the group is invisible to the pgid alone (#2309).
    tracker = descendant_tracker(root_pid=proc.pid, pgid=pgid)
    tracker.start()
    # False only while a drain thread still owns the streams, in which case that
    # thread closes them and this one must not touch them (see _drain_partial_output).
    owns_streams = True
    # Normal completion implies the group went with the child; only a teardown
    # that could not reach the group flips this, and then the sidecar is kept so
    # a later reaper can still find the survivors.
    group_gone = True
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        output = (stdout + stderr).strip()
        return proc.returncode == 0, output, proc.returncode, False
    except subprocess.TimeoutExpired as te:
        # Kill the whole process group before draining pipes so grandchildren
        # such as pytest-xdist workers cannot keep writing indefinitely after
        # the shell itself has timed out.
        group_gone = bool(_kill_process_group(proc))
        partial_out, drained = _drain_partial_output(te, proc)
        owns_streams = drained
        header = f"TIMEOUT after {timeout}s: {cmd}"
        if partial_out:
            return False, f"{header}\n{partial_out}", None, True
        return False, header, None, True
    except BaseException:
        group_gone = bool(_kill_process_group(proc))
        raise
    finally:
        # Drop the record only once the whole group is known to be gone. A
        # teardown that reached at most the direct child leaves grandchildren
        # running, and the sidecar is the only handle a later reaper has on
        # them — erasing it would strand them permanently (#2013). And a shell
        # that exited cleanly is not evidence its group did: `make gate` can
        # return while a pytest-xdist worker it started is still on the CPU, so
        # release checks and kills rather than assuming (#2309).
        teardown = release_group_record(
            pgid,
            group_killed=group_gone,
            sandbox_dir=str(cwd),
            lease=lease,
            tracker=tracker,
        )
        if teardown is not None and teardown_out is not None:
            teardown_out.append(teardown)
        if owns_streams:
            for stream_name in ("stdout", "stderr"):
                stream = getattr(proc, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass


def _run_shell(
    cmd: str,
    cwd: Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
    expected_python: str | None = None,
    *,
    teardown_out: list[ProcessTeardown] | None = None,
) -> tuple[bool, str]:
    """Run a shell command. Returns (success, combined output).

    ``teardown_out`` is threaded through to `_run_shell_detailed` for the callers
    that run a *project* command here rather than a short git query: the teardown
    happens either way, but only a caller with somewhere to put it can make the
    run's record say so (#2309).
    """
    ok, output, _exit_code, _timed_out = _run_shell_detailed(
        cmd,
        cwd,
        timeout=timeout,
        env=env,
        expected_python=expected_python,
        teardown_out=teardown_out,
    )
    return ok, output


# ── Worktree project-code evaluation ────────────────────────────────────


def _run_worktree_eval(
    workspace_path: Path,
    command: str,
    payload: dict,
    timeout: int = 120,
) -> dict:
    """Run a project-code evaluation in the worktree's Python environment.

    Prepends workspace_path/src to PYTHONPATH so the subprocess imports
    theforge.* modules from the worktree rather than the coordinator's copies.
    The checkout src that loaded this module is added as an explicit fallback
    for sparse synthetic worktrees used by tests; this keeps those subprocesses
    from falling through to whatever theforge happens to be installed in the
    ambient Python environment. This is the isolation boundary for self-hosting:
    project code is never imported into the coordinator's process; it runs only
    in this subprocess.

    Returns the parsed JSON result dict. Raises RuntimeError on subprocess
    failure.
    """
    env = os.environ.copy()
    worktree_src = str((workspace_path / "src").resolve())
    checkout_src = str(_CHECKOUT_SRC)
    existing = env.get("PYTHONPATH", "")
    pythonpath_entries = [worktree_src]
    if checkout_src != worktree_src:
        pythonpath_entries.append(checkout_src)
    if existing:
        pythonpath_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    result = _subprocess_run(
        [sys.executable, str(_SUBPROCESS_EVAL), command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(workspace_path),
        timeout=timeout,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"Worktree eval {command!r} failed (exit {result.returncode}): "
            f"{stderr or '(no stderr)'}"
        )

    return json.loads(result.stdout)
