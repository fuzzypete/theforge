"""Signal handling and post-run hook helpers for the coordinator."""

from __future__ import annotations

import os
import signal
import time
from typing import TYPE_CHECKING

from .log_tee import _end_run_log_tee, _safe_signal
from .state import RetryReason

if TYPE_CHECKING:
    from theforge.config import ForgeConfig
    from theforge.task import TaskStory

    from .logging import StructuredLogger
    from .state import CoordinatorResult, CoordinatorState


def _make_sigterm_handler(
    logger: "StructuredLogger",
    tee_state: "tuple[object, object] | None",
    prev_handler: object,
    state: "CoordinatorState | None" = None,
    task_start: float = 0.0,
    task: "TaskStory | None" = None,
    config: "ForgeConfig | None" = None,
) -> object:
    """Return a SIGTERM handler that emits run_end:crashed, closes the tee, and re-raises.

    Note on SIGKILL: SIGKILL (signal 9) cannot be intercepted by user-space code on
    any POSIX operating system — the kernel delivers it unconditionally without calling
    signal handlers. Crash diagnostics therefore cover SIGTERM only. SIGKILL kills are
    not observable by this handler.
    """

    def _handler(signum: int, frame: object) -> None:
        uptime = time.monotonic() - task_start
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)
        extra: dict[str, object] = {
            "signal": signum,
            "signal_name": sig_name,
            "uptime_seconds": round(uptime, 1),
        }
        if state is not None:
            extra["phase_at_crash"] = state.phase.name if state.phase is not None else "UNKNOWN"
            extra["iteration_at_crash"] = state.dev_iteration
            extra["cost_at_crash"] = round(state.total_cost, 6)
        extra["last_event"] = logger.last_event
        logger._safe_emit("run_end", outcome="crashed", **extra)
        if state is not None and task is not None and config is not None:
            try:
                from .notify import _ntfy_crash_notify  # noqa: PLC0415

                _ntfy_crash_notify(task, state, config, uptime)
            except Exception:
                pass
        _end_run_log_tee(tee_state)
        # Restore the previous handler before re-raising so we don't recurse.
        try:
            _safe_signal(signal.SIGTERM, prev_handler or signal.SIG_DFL)
        except Exception:
            pass
        os.kill(os.getpid(), signum)

    return _handler


def _set_timeout_resume(state: "CoordinatorState", gate_result: str) -> None:
    """Mark state for a timeout-resume retry with a short continuation prompt."""
    state.retry_reason = RetryReason.TIMEOUT_RESUME
    state.human_feedback = (
        "You were cut off by a timeout. Continue from where you left off. "
        f"Gate result: {gate_result}"
    )


def _fire_post_run_hook(
    config: "ForgeConfig",
    state: "CoordinatorState",
    task: "TaskStory",
    result: "CoordinatorResult",
    run_id: str,
    elapsed: float,
    logger: "StructuredLogger | None",
) -> None:
    """Fire the post_run lifecycle hook if configured. Best-effort; never raises."""
    if not (config.hooks and config.hooks.post_run):
        return
    from .hooks import build_post_run_payload
    from .hooks import run_hook as _run_hook

    _run_hook(
        config.hooks.post_run,
        build_post_run_payload(state, config, task, result, run_id, elapsed),
        config.hooks.timeout_seconds,
        "post_run",
        logger,
        secrets=config.secrets,
    )
