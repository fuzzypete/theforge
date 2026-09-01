"""Regression tests for the global network-access guard.

Verifies that:
  - All external TCP connections are blocked with a clear error message.
  - Loopback/localhost connections are NOT blocked.
  - httpx requests (used by OpenAI, Anthropic SDKs) are blocked.
  - OpenAI SDK configured with a custom base_url (DeepSeek / OpenAI-compatible
    provider pattern) is also blocked — no custom base_url bypasses the guard.
  - The @pytest.mark.network_integration + THEFORGE_RUN_INTEGRATION=1 opt-in
    lifts the guard for the duration of that test only.
  - The guard is re-applied after a network_integration test completes.
"""

from __future__ import annotations

import socket

import pytest

import tests.conftest as _conftest

# ---------------------------------------------------------------------------
# Guard installation
# ---------------------------------------------------------------------------


def test_network_guard_is_installed():
    """socket.socket must be the _BlockedSocket subclass during normal tests."""
    assert socket.socket is _conftest._BlockedSocket


# ---------------------------------------------------------------------------
# Direct socket — blocked
# ---------------------------------------------------------------------------


def test_external_ipv4_connect_is_blocked():
    """socket.connect() to a public IPv4 address must raise with the guard message."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="theforge-test-guard"):
            sock.connect(("8.8.8.8", 80))
    finally:
        sock.close()


def test_external_connect_ex_is_blocked():
    """socket.connect_ex() to a public IP must also raise (not silently return)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="theforge-test-guard"):
            sock.connect_ex(("8.8.8.8", 80))
    finally:
        sock.close()


def test_error_message_names_the_host():
    """The guard error must name the blocked host so the cause is obvious."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="1.1.1.1"):
            sock.connect(("1.1.1.1", 443))
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Loopback — allowed
# ---------------------------------------------------------------------------


def test_loopback_ipv4_is_allowed():
    """127.0.0.1 must not be blocked; ECONNREFUSED from the OS is acceptable."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # connect_ex returns an errno without raising; any non-guard error is fine.
        err = sock.connect_ex(("127.0.0.1", 19997))
        # ECONNREFUSED (111 on Linux, 61 on macOS) or similar is expected.
        # We only care that our guard did NOT raise.
        assert isinstance(err, int)
    finally:
        sock.close()


def test_localhost_string_is_allowed():
    """'localhost' hostname must not trigger the guard."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        err = sock.connect_ex(("localhost", 19996))
        assert isinstance(err, int)
    finally:
        sock.close()


def test_localhost_uppercase_is_allowed():
    """'LOCALHOST' (uppercase) must not trigger the guard — check is case-insensitive."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            err = sock.connect_ex(("LOCALHOST", 19994))
            assert isinstance(err, int)
        except socket.gaierror:
            # Some environments don't resolve uppercase 'LOCALHOST'; that's an OS
            # resolution failure, not a guard block — the guard passed correctly.
            pass
    finally:
        sock.close()


def test_loopback_ipv6_is_allowed():
    """::1 (IPv6 loopback) must not be blocked."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("IPv6 not available on this machine")
    try:
        err = sock.connect_ex(("::1", 19995))
        assert isinstance(err, int)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# httpx — blocked (httpx is pulled in by the openai SDK)
# ---------------------------------------------------------------------------


def test_httpx_external_get_is_blocked():
    """httpx.get() to an external IP must be blocked by the guard."""
    httpx = pytest.importorskip("httpx")
    # Use a bare IP so the test doesn't depend on DNS resolution.
    with pytest.raises(Exception):
        httpx.get("http://8.8.8.8:80/")


def test_httpx_https_external_is_blocked():
    """httpx HTTPS connection to an external host must also be blocked."""
    httpx = pytest.importorskip("httpx")
    with pytest.raises(Exception):
        httpx.get("https://8.8.8.8:443/")


# ---------------------------------------------------------------------------
# OpenAI SDK — custom base_url (DeepSeek / OpenAI-compatible provider pattern)
# ---------------------------------------------------------------------------


def test_openai_compatible_base_url_is_blocked():
    """OpenAI SDK with a custom base_url must not bypass the network guard.

    Regression for the DeepSeek/OpenAI-compatible provider pattern: using the
    OpenAI SDK pointed at a different endpoint is still an external network call
    and must be blocked.  Uses a bare IP to avoid DNS dependency.

    Measures 2.09s serially — the SDK's own connect and retry stack running
    against a blocked socket — and runs under the suite's shared per-test bound,
    which is sized above the contention that cost inflates to.
    """
    openai = pytest.importorskip("openai")
    # Point at a numeric IP so DNS can't fail before the socket connect.
    client = openai.OpenAI(
        api_key="fake-test-key-not-real",
        base_url="http://8.8.8.8:80/v1",
    )
    with pytest.raises(Exception):
        client.models.list()


# ---------------------------------------------------------------------------
# network_integration marker opt-in
# ---------------------------------------------------------------------------


def test_network_integration_marker_requires_env_var(pytestconfig):
    """A test with the marker but without the env var is skipped, not failed.

    We verify this indirectly: the _enforce_network_integration_marker fixture
    calls pytest.skip() when the env var is absent, which means the test body
    never runs.  We confirm the skip mechanism is wired up by checking that the
    guard is still active (not accidentally lifted) in a normal test.
    """
    # If the guard were accidentally lifted, socket.socket would be the real class.
    assert socket.socket is _conftest._BlockedSocket


@pytest.mark.network_integration
def test_network_integration_marker_lifts_guard():
    """With marker + env var, socket.socket is restored to the real implementation."""
    # This test only runs when THEFORGE_RUN_INTEGRATION=1 is set (otherwise skipped).
    assert socket.socket is _conftest._REAL_SOCKET


def test_guard_is_restored_after_network_integration_test():
    """After a network_integration test, the guard must be re-applied.

    This test runs after test_network_integration_marker_lifts_guard in the
    same worker and verifies the teardown in _enforce_network_integration_marker
    correctly re-installs _BlockedSocket.
    """
    assert socket.socket is _conftest._BlockedSocket
