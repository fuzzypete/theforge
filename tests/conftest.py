"""Global test fixtures and safety patches for the theforge test suite."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket as _socket_module
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config import (
    SCRUBBED_CLI_LAUNCHERS,
    SCRUBBED_ENV_VARS,
    SCRUBBED_HOME_PATHS,
    ModelProfile,
)

# ---------------------------------------------------------------------------
# Global network guard — installed at conftest load time
# ---------------------------------------------------------------------------

_REAL_SOCKET = _socket_module.socket

_GUARD_MSG = (
    "[theforge-test-guard] Blocked external network connection to {host!r}. "
    "Unit tests must not make real network calls. "
    "Add @pytest.mark.network_integration and set THEFORGE_RUN_INTEGRATION=1 "
    "to opt in to real network access."
)


def _is_loopback(host: str) -> bool:
    """Return True if *host* is a loopback or localhost address."""
    if host.lower() in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _BlockedSocket(_REAL_SOCKET):
    """socket.socket subclass that refuses non-loopback outbound connections.

    Installed unconditionally at conftest load time so every test worker
    starts with the guard active.  AF_UNIX sockets (file-path addresses)
    are always allowed; only TCP/UDP tuples are checked.
    """

    def _check_address(self, address: object) -> None:
        if isinstance(address, tuple):
            host = str(address[0])
            if not _is_loopback(host):
                raise OSError(_GUARD_MSG.format(host=host))

    def connect(self, address):  # type: ignore[override]
        self._check_address(address)
        return super().connect(address)

    def connect_ex(self, address):  # type: ignore[override]
        self._check_address(address)
        return super().connect_ex(address)

    def sendto(self, data, *args):  # type: ignore[override]
        # sendto(data, address) or sendto(data, flags, address)
        address = args[-1]
        self._check_address(address)
        return super().sendto(data, *args)

    def sendmsg(self, buffers, ancdata=(), flags=0, address=None):  # type: ignore[override]
        if address is not None:
            self._check_address(address)
        return super().sendmsg(buffers, ancdata, flags, address)


# Install the guard before any test module is imported.
_socket_module.socket = _BlockedSocket


def _gate_scrub_enabled() -> bool:
    return os.environ.get("THEFORGE_ALLOW_AGENT_CREDS") != "1"


_REAL_WHICH = shutil.which


@pytest.fixture(scope="session", autouse=True)
def _scrub_agent_credentials_for_gate():
    """Strip agent credentials/auth state unless explicitly opted out.

    This mirrors the Makefile gate scrub so direct pytest runs in a dirty shell
    still enforce the same minimum isolation contract.
    """
    if not _gate_scrub_enabled():
        yield
        return

    stripped = sorted(name for name in SCRUBBED_ENV_VARS if name in os.environ)
    original_env = {name: os.environ.get(name) for name in SCRUBBED_ENV_VARS}
    original_home = os.environ.get("HOME")
    scrubbed_launchers = frozenset(SCRUBBED_CLI_LAUNCHERS)

    def _scrubbed_which(cmd, mode: int = os.F_OK | os.X_OK, path: str | None = None):
        if os.path.basename(os.fsdecode(cmd)) in scrubbed_launchers:
            return None
        return _REAL_WHICH(cmd, mode=mode, path=path)

    for name in SCRUBBED_ENV_VARS:
        os.environ.pop(name, None)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"

    with tempfile.TemporaryDirectory(prefix="theforge-gate-home-") as tmp_home:
        scrubbed_home = Path(tmp_home)
        for rel_path in SCRUBBED_HOME_PATHS:
            target = scrubbed_home / rel_path
            if rel_path.suffix:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            else:
                target.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(scrubbed_home)
        print(
            "[theforge-test-guard] scrubbed agent credentials: "
            + (", ".join(stripped) if stripped else "none")
        )
        with patch("shutil.which", side_effect=_scrubbed_which):
            try:
                yield
            finally:
                if original_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = original_home
                for name, value in original_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value


@pytest.fixture(autouse=True)
def _enforce_network_integration_marker(request):
    """Enforce the two-key opt-in for real-network tests.

    * No marker → test runs with the socket guard active (the common case).
    * Marker present, THEFORGE_RUN_INTEGRATION not set → test is skipped.
    * Marker present AND THEFORGE_RUN_INTEGRATION=1 → guard is lifted for
      the duration of this test only, then re-applied on teardown.
    """
    marker = request.node.get_closest_marker("network_integration")
    if marker is None:
        yield
        return

    if not os.environ.get("THEFORGE_RUN_INTEGRATION"):
        pytest.skip("set THEFORGE_RUN_INTEGRATION=1 to run network_integration tests")

    # Both conditions met — lift the guard for this test only.
    _socket_module.socket = _REAL_SOCKET
    try:
        yield
    finally:
        _socket_module.socket = _BlockedSocket


@pytest.fixture(autouse=True)
def _enforce_cli_contract_marker(request, monkeypatch):
    """Gate cli_contract tests behind THEFORGE_RUN_CLI_CONTRACT=1.

    When the marker is present and the env gate is set, lift the network guard
    and the coordinator-runner blockers and the credential scrub so the test
    can spawn the real provider CLI to validate argv parsing.
    """
    marker = request.node.get_closest_marker("cli_contract")
    if marker is None:
        yield
        return

    if not os.environ.get("THEFORGE_RUN_CLI_CONTRACT"):
        pytest.skip("set THEFORGE_RUN_CLI_CONTRACT=1 to run cli_contract tests")

    _socket_module.socket = _REAL_SOCKET
    monkeypatch.setattr("shutil.which", _REAL_WHICH)
    try:
        yield
    finally:
        _socket_module.socket = _BlockedSocket


def require_cli(name: str) -> str:
    """Return the absolute path to *name*, or skip the test if not on PATH."""
    path = _REAL_WHICH(name)
    if not path:
        pytest.skip(f"{name!r} not installed on PATH")
    return path


@pytest.fixture
def dev_profile() -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=2.0,
        timeout_seconds=900,
        allowed_tools=("Read", "Edit", "Write", "Bash"),
    )


@pytest.fixture
def review_profile() -> ModelProfile:
    return ModelProfile(
        name="review",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


@pytest.fixture
def codex_profile() -> ModelProfile:
    return ModelProfile(
        name="codex-reviewer",
        cli="codex",
        model="o4-mini",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )


@pytest.fixture
def gemini_profile() -> ModelProfile:
    return ModelProfile(
        name="gemini-reviewer",
        cli="gemini",
        model="gemini-2.5-pro",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
        sandbox_mode="none",  # sandbox behaviour is tested in test_runner_gemini.py
    )


@pytest.fixture(autouse=True)
def _isolate_log_level():
    """Prevent log-level mutations from leaking across tests.

    Resets both the top-level theforge.log_level._LOG_LEVEL global and the
    separate copy in theforge.coordinator.util (which has its own module-level
    _LOG_LEVEL that coordinator code reads directly).
    """
    import theforge.coordinator.util as _cu_mod
    import theforge.log_level as _ll_mod

    original_ll = _ll_mod._LOG_LEVEL
    original_cu = _cu_mod._LOG_LEVEL
    yield
    _ll_mod.set_log_level(original_ll)
    _cu_mod.set_log_level(original_cu)


@pytest.fixture(autouse=True)
def _block_real_notifications():
    """Prevent any test from firing real OS or ntfy notifications.

    Patches both the legacy coord_notify._notify path and the
    notify_backends._send_terminal path (used by send_notifications),
    so no osascript / notify-send calls escape into the OS during tests.
    """
    with (
        patch("theforge.coordinator.notify._notify"),
        patch("theforge.coordinator.notify._ntfy_publish"),
        patch("theforge.coordinator.remote_gates._ntfy_publish"),
        patch("theforge.sprint.runner._notify"),
        patch("theforge.sprint.runner._ntfy_publish"),
        # notify_backends.send_notifications dispatches to _send_terminal
        # directly (not via coord_notify._notify), so patch it here too.
        patch("theforge.notify_backends._send_terminal"),
        patch("theforge.notify_backends._send_ntfy"),
        patch("theforge.notify_backends._send_webhook"),
        patch("theforge.notify_backends._send_slack"),
    ):
        yield


@pytest.fixture(autouse=True)
def _block_real_coordinator_runners():
    """Require tests to stub coordinator runner entry points explicitly.

    Accidental fallthrough into any coordinator runner path is both slow and
    misleading: it can spawn actual runner processes and watchdog threads from
    tests that only intended to exercise coordinator control flow.

    Tests that genuinely need one of these boundaries should patch that symbol
    explicitly in the test body or decorator stack.

    Lifted when ``THEFORGE_ALLOW_AGENT_CREDS=1`` so that
    ``make test-integration`` and other opt-in suites can exercise real
    coordinator/runner paths end-to-end.
    """
    if not _gate_scrub_enabled():
        yield
        return

    def _unexpected_call(symbol: str) -> AssertionError:
        return AssertionError(
            f"real agent call blocked by gate scrub: {symbol}; patch {symbol} explicitly."
        )

    with (
        patch(
            "theforge.coordinator.review_pool.run_agent_pool",
            side_effect=_unexpected_call("theforge.coordinator.review_pool.run_agent_pool"),
        ),
        patch(
            "theforge.coordinator.review_pool.run_agent",
            side_effect=_unexpected_call("theforge.coordinator.review_pool.run_agent"),
        ),
        patch(
            "theforge.coordinator.plan_flow.run_agent_pool",
            side_effect=_unexpected_call("theforge.coordinator.plan_flow.run_agent_pool"),
        ),
        patch(
            "theforge.coordinator.plan_flow.run_agent",
            side_effect=_unexpected_call("theforge.coordinator.plan_flow.run_agent"),
        ),
        patch(
            "theforge.coordinator.preflight_flow.run_agent",
            side_effect=_unexpected_call("theforge.coordinator.preflight_flow.run_agent"),
        ),
        patch(
            "theforge.coordinator.dev_phase.run_agent",
            side_effect=_unexpected_call("theforge.coordinator.dev_phase.run_agent"),
        ),
        patch(
            "theforge.coordinator.workspace.run_agent",
            side_effect=_unexpected_call("theforge.coordinator.workspace.run_agent"),
        ),
        patch(
            "theforge.ideate.run_agent",
            side_effect=_unexpected_call("theforge.ideate.run_agent"),
        ),
        patch(
            "theforge.ideate.run_agent_pool",
            side_effect=_unexpected_call("theforge.ideate.run_agent_pool"),
        ),
        patch(
            "theforge.cli.providers.run_api_agent",
            side_effect=_unexpected_call("theforge.cli.providers.run_api_agent"),
        ),
        patch(
            "theforge.runners.run_agent",
            side_effect=_unexpected_call("theforge.runners.run_agent"),
        ),
        patch(
            "theforge.runners.run_agent_pool",
            side_effect=_unexpected_call("theforge.runners.run_agent_pool"),
        ),
        patch(
            "theforge.runners.run_api_agent",
            side_effect=_unexpected_call("theforge.runners.run_api_agent"),
        ),
    ):
        yield
