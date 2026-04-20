"""Tests for _run_shell timeout behavior: partial output capture, prefix format,
and non-blocking drain after process kill."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.coordinator import util as coord_util
from theforge.coordinator.util import _run_shell


class TestRunShellTimeout:
    """Verify _run_shell timeout handling captures partial output safely."""

    def test_timeout_preserves_partial_output(self, tmp_path: Path) -> None:
        """A process that prints then hangs should have its output captured."""
        # Print a marker, flush, then sleep forever.
        cmd = (
            'python3 -u -c "'
            "import sys, time; "
            "sys.stdout.write('PARTIAL_MARKER\\n'); "
            "sys.stdout.flush(); "
            'time.sleep(300)"'
        )
        ok, output = _run_shell(cmd, tmp_path, timeout=0.2)

        assert ok is False
        assert output.startswith("TIMEOUT after 0.2s:")
        assert "PARTIAL_MARKER" in output

    def test_timeout_handler_does_not_block(self, tmp_path: Path) -> None:
        """The timeout path must return promptly — not block on pipe reads."""
        # Process that writes nothing and sleeps forever.
        cmd = 'python3 -c "import time; time.sleep(300)"'
        start = time.monotonic()
        ok, output = _run_shell(cmd, tmp_path, timeout=0.1)
        elapsed = time.monotonic() - start

        assert ok is False
        assert output.startswith("TIMEOUT after 0.1s:")
        # Should return within a few seconds of the timeout, not hang.
        # Allow generous slack (5s) for CI variance, but catch infinite blocks.
        assert elapsed < 5, f"Timeout handler took {elapsed:.1f}s — possible pipe read block"

    def test_timeout_prefix_format(self, tmp_path: Path) -> None:
        """Output must start with 'TIMEOUT after <N>s:' for gate.py sentinel."""
        cmd = 'python3 -c "import time; time.sleep(300)"'
        ok, output = _run_shell(cmd, tmp_path, timeout=0.1)

        assert ok is False
        # gate.py relies on output.startswith("TIMEOUT") to detect timeouts.
        assert output.startswith("TIMEOUT after 0.1s:")

    def test_timeout_captures_stderr(self, tmp_path: Path) -> None:
        """Partial stderr should also be captured on timeout."""
        cmd = (
            'python3 -u -c "'
            "import sys, time; "
            "sys.stderr.write('STDERR_MARKER\\n'); "
            "sys.stderr.flush(); "
            'time.sleep(300)"'
        )
        ok, output = _run_shell(cmd, tmp_path, timeout=0.2)

        assert ok is False
        assert "STDERR_MARKER" in output


def test_run_shell_defaults_to_workspace_venv_env(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    proc = MagicMock()
    proc.communicate.return_value = ("ok\n", "")
    proc.returncode = 0
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    with patch("theforge.coordinator.util.subprocess.Popen", return_value=proc) as mock_popen:
        ok, output = coord_util._run_shell("python -V", tmp_path)

    env_passed = mock_popen.call_args[1]["env"]
    assert ok is True
    assert output == "ok"
    assert env_passed["PATH"].split(os.pathsep)[0] == str(venv_bin)
    assert env_passed["VIRTUAL_ENV"] == str(tmp_path / ".venv")
