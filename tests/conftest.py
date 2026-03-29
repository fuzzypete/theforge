"""Global test fixtures and safety patches for the theforge test suite."""

from __future__ import annotations

from unittest.mock import patch

import pytest


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
