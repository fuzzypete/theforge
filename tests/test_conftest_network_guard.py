"""Regression tests for the global network guard in conftest.py.

Verifies that TCP (connect/connect_ex) and UDP (sendto/sendmsg) calls to
non-loopback addresses are blocked, while loopback addresses are not.
"""

from __future__ import annotations

import socket

import pytest

# External address used in all guard-block assertions.
_EXTERNAL = ("8.8.8.8", 53)
# Loopback addresses that the guard must allow through.
_LOOPBACKS = [
    ("127.0.0.1", 9999),
    ("::1", 9999),
    ("localhost", 9999),
]


def _udp_sock() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _tcp_sock() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


# ---------------------------------------------------------------------------
# TCP — connect / connect_ex
# ---------------------------------------------------------------------------


def test_connect_external_blocked():
    sock = _tcp_sock()
    try:
        with pytest.raises(OSError, match="Blocked external network"):
            sock.connect(_EXTERNAL)
    finally:
        sock.close()


def test_connect_ex_external_blocked():
    sock = _tcp_sock()
    try:
        with pytest.raises(OSError, match="Blocked external network"):
            sock.connect_ex(_EXTERNAL)
    finally:
        sock.close()


@pytest.mark.parametrize("addr", _LOOPBACKS)
def test_connect_loopback_not_blocked(addr):
    """Guard must not raise for loopback addresses (connection itself may fail)."""
    sock = _tcp_sock()
    try:
        # connect() to a port with nothing listening raises ConnectionRefused,
        # NOT our guard error.  Either way, our guard message must be absent.
        try:
            sock.connect(addr)
        except OSError as exc:
            assert "Blocked external network" not in str(exc)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# UDP — sendto
# ---------------------------------------------------------------------------


def test_sendto_external_blocked():
    sock = _udp_sock()
    try:
        with pytest.raises(OSError, match="Blocked external network"):
            sock.sendto(b"ping", _EXTERNAL)
    finally:
        sock.close()


def test_sendto_with_flags_external_blocked():
    """sendto(data, flags, address) form must also be blocked."""
    sock = _udp_sock()
    try:
        with pytest.raises(OSError, match="Blocked external network"):
            sock.sendto(b"ping", 0, _EXTERNAL)
    finally:
        sock.close()


@pytest.mark.parametrize("addr", [("127.0.0.1", 9999), ("localhost", 9999)])
def test_sendto_loopback_not_blocked(addr):
    """Guard must not raise for loopback UDP destinations."""
    sock = _udp_sock()
    try:
        # sendto to a loopback address with nothing listening is fine at the
        # socket layer — the guard must not raise its specific error.
        try:
            sock.sendto(b"ping", addr)
        except OSError as exc:
            assert "Blocked external network" not in str(exc)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# UDP — sendmsg (POSIX only; not available on Windows)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="sendmsg not available")
def test_sendmsg_external_blocked():
    sock = _udp_sock()
    try:
        with pytest.raises(OSError, match="Blocked external network"):
            sock.sendmsg([b"ping"], [], 0, _EXTERNAL)
    finally:
        sock.close()


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="sendmsg not available")
def test_sendmsg_no_address_not_blocked():
    """sendmsg without an address (connected socket) must not trigger guard."""
    sock = _udp_sock()
    try:
        # Without address= the guard has nothing to check; the call may fail
        # for other reasons (not connected), but not with our guard message.
        try:
            sock.sendmsg([b"ping"])
        except OSError as exc:
            assert "Blocked external network" not in str(exc)
    finally:
        sock.close()


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="sendmsg not available")
def test_sendmsg_loopback_not_blocked():
    """Guard must not raise for loopback sendmsg destinations."""
    sock = _udp_sock()
    try:
        try:
            sock.sendmsg([b"ping"], [], 0, ("127.0.0.1", 9999))
        except OSError as exc:
            assert "Blocked external network" not in str(exc)
    finally:
        sock.close()
