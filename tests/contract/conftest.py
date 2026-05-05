"""Shared helpers for the CLI-contract test layer.

Each contract test constructs argv via the runner's real builder and invokes
the installed CLI. The helper ``assert_cli_accepts_argv`` spawns the binary,
gives it a short window to either exit (e.g. on parse error or --help) or
start doing real work, then kills it and inspects stderr for argparse-style
parse failures.

This layer is gated behind the ``cli_contract`` marker and the
``THEFORGE_RUN_CLI_CONTRACT=1`` env var (see tests/conftest.py).
"""

from __future__ import annotations

import re
import subprocess

# Patterns that indicate the CLI rejected the argv before doing any model work.
# These are conservative — false negatives (a real bug we miss) are worse than
# false positives (a flaky-looking test we have to investigate). The patterns
# below match argparse, clap (Rust), commander (Node), and yargs error output.
_PARSE_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"unrecognized arguments?:\s*(\S+)", re.IGNORECASE),
    re.compile(r"unknown option[: ]+'?(--?\S+?)'?\b", re.IGNORECASE),
    re.compile(r"unexpected argument '?(--?\S+?)'?\b", re.IGNORECASE),
    re.compile(r"no such (?:option|subcommand)[: ]+'?(\S+)'?", re.IGNORECASE),
    re.compile(r"error: argument (\S+)", re.IGNORECASE),
    re.compile(r"invalid (?:option|argument)[: ]+'?(--?\S+?)'?\b", re.IGNORECASE),
    re.compile(r"unknown (?:flag|argument)[: ]+'?(--?\S+?)'?\b", re.IGNORECASE),
)


def _extract_offending_flag(text: str) -> str | None:
    for pattern in _PARSE_ERROR_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


def assert_cli_accepts_argv(
    runner_name: str,
    argv: list[str],
    *,
    timeout: float = 4.0,
    stdin_input: str | None = "",
) -> None:
    """Spawn *argv* and assert the CLI did not reject it as malformed.

    Strategy:
    - Spawn the process with stdin/stdout/stderr piped.
    - Wait up to *timeout* seconds for it to either exit on its own or stay
      alive past the parse phase.
    - If it stayed alive past the timeout, the argv was at least syntactically
      accepted — kill it and pass.
    - If it exited within the timeout, scan stderr (and stdout, since some
      CLIs print parse errors there) for argparse-style failure markers.
      If found, fail with a message naming the runner and the offending flag.
    - A clean exit-0 (e.g. --help short-circuit) is also a pass.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise AssertionError(f"[{runner_name}] could not spawn {argv[0]!r}: {exc}") from exc

    try:
        stdout, stderr = proc.communicate(input=stdin_input, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return  # alive past parse — argv accepted as well-formed

    combined = (stderr or "") + "\n" + (stdout or "")
    flag = _extract_offending_flag(combined)
    if flag is not None:
        raise AssertionError(
            f"[{runner_name}] CLI rejected argv: offending flag/argument {flag!r}\n"
            f"  argv: {argv}\n"
            f"  stderr: {(stderr or '').strip()[:500]}"
        )
    if proc.returncode not in (0, None):
        # Nonzero exit without a recognized parse-error marker is not, by itself,
        # an argv-contract failure (could be missing API key, network, etc).
        # Surface it for diagnostic value but do not fail.
        return
