"""Tests for the pluggable notification backend dispatch."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from theforge.config import BackendConfig
from theforge.notify_backends import (
    _send_ntfy,
    _send_terminal,
    _send_webhook,
    send_notifications,
)


def _make_config(backends: list[BackendConfig]) -> MagicMock:
    """Build a minimal ForgeConfig mock with specified backends."""
    config = MagicMock()
    config.notifications.backends = backends
    return config


def test_send_notifications_dispatches_to_all_backends():
    config = _make_config(
        [
            BackendConfig(type="terminal"),
            BackendConfig(type="webhook", url="http://example.com/hook"),
        ]
    )
    with (
        patch("theforge.notify_backends._send_terminal") as mock_terminal,
        patch("theforge.notify_backends._send_webhook") as mock_webhook,
    ):
        send_notifications(config, "title", "body")
        mock_terminal.assert_called_once_with("title", "body")
        mock_webhook.assert_called_once_with("http://example.com/hook", "title", "body")


def test_send_notifications_skips_ntfy_without_url():
    config = _make_config([BackendConfig(type="ntfy", url=None)])
    with patch("theforge.notify_backends._send_ntfy") as mock_ntfy:
        send_notifications(config, "title", "body")
        mock_ntfy.assert_not_called()


def test_send_notifications_calls_ntfy_with_url():
    config = _make_config(
        [BackendConfig(type="ntfy", url="https://ntfy.sh/topic", priority="high")]
    )
    with patch("theforge.notify_backends._send_ntfy") as mock_ntfy:
        send_notifications(config, "title", "body")
        mock_ntfy.assert_called_once_with("https://ntfy.sh/topic", "high", "title", "body")


def test_send_notifications_skips_webhook_without_url():
    config = _make_config([BackendConfig(type="webhook", url=None)])
    with patch("theforge.notify_backends._send_webhook") as mock_wh:
        send_notifications(config, "title", "body")
        mock_wh.assert_not_called()


def test_backend_failure_logs_warning_and_continues():
    config = _make_config(
        [
            BackendConfig(type="terminal"),
            BackendConfig(type="terminal"),
        ]
    )
    call_count = 0

    def _fail_first(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")

    with patch("theforge.notify_backends._send_terminal", side_effect=_fail_first):
        # Should not raise even though the first backend fails
        send_notifications(config, "title", "body")

    assert call_count == 2


def test_multiple_backends_all_fire_even_if_one_fails():
    fired = []

    def _terminal(*args):
        raise RuntimeError("terminal failed")

    def _webhook(*args):
        fired.append("webhook")

    config = _make_config(
        [
            BackendConfig(type="terminal"),
            BackendConfig(type="webhook", url="http://example.com"),
        ]
    )
    with (
        patch("theforge.notify_backends._send_terminal", side_effect=_terminal),
        patch("theforge.notify_backends._send_webhook", side_effect=_webhook),
    ):
        send_notifications(config, "title", "body")

    assert "webhook" in fired


def test_send_terminal_macos_calls_osascript():
    with (
        patch("theforge.notify_backends.platform.system", return_value="Darwin"),
        patch("theforge.notify_backends.shutil.which", return_value="/usr/bin/osascript"),
        patch("theforge.notify_backends.subprocess.run") as mock_run,
    ):
        _send_terminal("Test Title", "Test Body")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"


def test_send_terminal_linux_calls_notify_send():
    with (
        patch("theforge.notify_backends.platform.system", return_value="Linux"),
        patch("theforge.notify_backends.shutil.which", return_value="/usr/bin/notify-send"),
        patch("theforge.notify_backends.subprocess.run") as mock_run,
    ):
        _send_terminal("Title", "Body")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "notify-send"


def test_send_ntfy_calls_ntfy_publish():
    with patch("theforge.coord_notify._ntfy_publish") as mock_pub:
        _send_ntfy("https://ntfy.sh/topic", "high", "Title", "Body")
        mock_pub.assert_called_once_with("https://ntfy.sh/topic", "Title", "Body", priority="high")


def test_send_webhook_posts_json():

    class _MockResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch(
        "theforge.notify_backends.urllib.request.urlopen", return_value=_MockResponse()
    ) as mock_open:
        _send_webhook("http://example.com/hook", "Title", "Body")
        assert mock_open.called
        req = mock_open.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["title"] == "Title"
        assert payload["body"] == "Body"
        assert "timestamp" in payload
        assert req.get_header("Content-type") == "application/json"


def test_send_notifications_unknown_backend_logs_warning():
    config = _make_config([BackendConfig(type="unknown")])
    # Should not raise
    send_notifications(config, "title", "body")
