"""Shared log-level enum used by both the runners and coordinator packages."""

from __future__ import annotations

from enum import IntEnum


class LogLevel(IntEnum):
    PROGRESS = 0  # default: phase transitions, agent start/done, verdicts
    VERBOSE = 1  # adds tool activity, heartbeats, raw output


_LOG_LEVEL: LogLevel = LogLevel.PROGRESS


def set_log_level(level: LogLevel) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level
