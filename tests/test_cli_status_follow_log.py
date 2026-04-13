"""Tests for _follow_log_with_redirect partial-line handling."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from theforge.cli.status import _SENTINEL_EOF, _follow_log_with_redirect


class TestFollowLogPartialLine:
    def test_partial_line_not_printed_until_complete(self, tmp_path: Path, capsys) -> None:
        """A line without a trailing newline must not be printed; wait for the full line."""
        log = tmp_path / "run-abc.log"

        # Write a partial line first, then complete it after a short delay.
        log.write_text("")

        def _write_log():
            time.sleep(0.05)
            with open(log, "a") as f:
                f.write("partial")
            time.sleep(0.1)
            with open(log, "a") as f:
                f.write(" complete\n")
            time.sleep(0.05)
            with open(log, "a") as f:
                f.write(f"{_SENTINEL_EOF}\n")

        writer = threading.Thread(target=_write_log, daemon=True)
        writer.start()

        result = _follow_log_with_redirect(log, "other-run-id")
        writer.join(timeout=3)

        captured = capsys.readouterr()
        assert result is None  # sentinel EOF reached
        assert "partial complete" in captured.out
        # The partial text must not appear on its own line before the rest arrives.
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert any("partial complete" in ln for ln in lines)

    def test_complete_lines_printed_immediately(self, tmp_path: Path, capsys) -> None:
        """Lines with trailing newlines are printed without extra delay."""
        log = tmp_path / "run-xyz.log"
        log.write_text(f"line one\nline two\n{_SENTINEL_EOF}\n")

        result = _follow_log_with_redirect(log, "other-run-id")

        assert result is None
        captured = capsys.readouterr()
        assert "line one" in captured.out
        assert "line two" in captured.out

    def test_partial_line_does_not_split_output(self, tmp_path: Path, capsys) -> None:
        """A partial line followed by its completion must produce a single output line."""
        log = tmp_path / "run-split.log"
        log.write_text("")

        def _write_log():
            time.sleep(0.05)
            with open(log, "a") as f:
                f.write("begin")
            time.sleep(0.1)
            with open(log, "a") as f:
                f.write("-end\n")
            time.sleep(0.05)
            with open(log, "a") as f:
                f.write(f"{_SENTINEL_EOF}\n")

        writer = threading.Thread(target=_write_log, daemon=True)
        writer.start()
        _follow_log_with_redirect(log, "other-run-id")
        writer.join(timeout=3)

        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        # "begin" and "-end" must appear together on one line, not on separate lines.
        assert any(ln == "begin-end" for ln in lines), f"lines: {lines}"
        assert not any(ln == "begin" for ln in lines), f"'begin' appeared alone: {lines}"
