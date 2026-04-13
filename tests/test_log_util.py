"""Tests for the shared log-line emitter in theforge.log_util."""

from __future__ import annotations

import re

from theforge.log_util import _log_line

_TS_RE = re.compile(r"\d{2}:\d{2}:\d{2}\.\d{3}")


def test_forge_tag_present(capsys: object) -> None:
    _log_line("[forge]", "hello")
    captured = capsys.readouterr()  # type: ignore[union-attr]
    line = captured.err.rstrip("\n")
    assert line.startswith("[forge]"), f"Expected [forge] prefix, got: {line!r}"


def test_timestamp_format(capsys: object) -> None:
    _log_line("[forge]", "hello")
    captured = capsys.readouterr()  # type: ignore[union-attr]
    line = captured.err.rstrip("\n")
    parts = line.split(" ", 2)
    # parts[0] = "[forge]", parts[1] = "HH:MM:SS.mmm", parts[2] = "hello"
    assert len(parts) == 3, f"Expected 3 space-separated parts, got: {line!r}"
    assert _TS_RE.fullmatch(parts[1]), f"Timestamp {parts[1]!r} does not match HH:MM:SS.mmm"


def test_message_at_end(capsys: object) -> None:
    _log_line("[forge]", "hello")
    captured = capsys.readouterr()  # type: ignore[union-attr]
    line = captured.err.rstrip("\n")
    assert line.endswith("hello"), f"Expected line to end with 'hello', got: {line!r}"


def test_sprint_tag(capsys: object) -> None:
    _log_line("[sprint]", "world")
    captured = capsys.readouterr()  # type: ignore[union-attr]
    line = captured.err.rstrip("\n")
    assert line.startswith("[sprint]"), f"Expected [sprint] prefix, got: {line!r}"
    parts = line.split(" ", 2)
    assert len(parts) == 3
    assert _TS_RE.fullmatch(parts[1]), f"Timestamp {parts[1]!r} does not match HH:MM:SS.mmm"
    assert line.endswith("world"), f"Expected line to end with 'world', got: {line!r}"


def test_message_with_spaces(capsys: object) -> None:
    _log_line("[forge]", "a message with spaces")
    captured = capsys.readouterr()  # type: ignore[union-attr]
    line = captured.err.rstrip("\n")
    assert line.endswith("a message with spaces"), f"Full message not preserved: {line!r}"
