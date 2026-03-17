"""Tests for the traces.write_trace helper."""

from theforge.traces import write_trace


def test_write_trace_creates_file(tmp_path):
    target = tmp_path / "traces" / "1-dev-prompt.txt"
    write_trace(target, "hello prompt")
    assert target.read_text(encoding="utf-8") == "hello prompt"


def test_write_trace_creates_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "trace.txt"
    write_trace(target, "content")
    assert target.exists()


def test_write_trace_overwrites_existing(tmp_path):
    target = tmp_path / "plan.txt"
    write_trace(target, "first")
    write_trace(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_write_trace_failure_logs_warning(tmp_path, capsys):
    # Pass a path whose parent is a file (not a dir) to trigger an error.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    bad_path = blocker / "child.txt"

    # Should not raise — failure is best-effort.
    write_trace(bad_path, "data")

    # Warning should have been emitted via _cu._log (which prints to stderr).
    captured = capsys.readouterr()
    assert "Warning: trace write failed" in captured.err


def test_write_trace_empty_content(tmp_path):
    target = tmp_path / "empty.txt"
    write_trace(target, "")
    assert target.read_text(encoding="utf-8") == ""
