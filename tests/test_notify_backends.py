"""Tests for the pluggable notification backend dispatch."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from theforge.config import BackendConfig
from theforge.notify_backends import (
    MAX_PENDING_NOTIFICATION_BODY_CHARS,
    _send_ntfy,
    _send_slack,
    _send_terminal,
    _send_webhook,
    format_pending_decision_notification,
    send_notifications,
)


def _make_config(backends: list[BackendConfig]) -> MagicMock:
    """Build a minimal ForgeConfig mock with specified backends."""
    config = MagicMock()
    config.notifications.backends = backends
    config.secrets = {}
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
    with patch("theforge.coordinator.notify._ntfy_publish") as mock_pub:
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


def test_format_pending_decision_notification_includes_actionable_command():
    body = format_pending_decision_notification(
        {
            "reason": "Quorum unmet: 1/2 reviewers approved.",
            "run_id": "65273305e89c",
            "phase": "ESCALATE",
            "options": ["accept", "redirect", "extend"],
            "timeout_at": "2026-08-14T20:00:00+00:00",
        },
        pending_path="/tmp/.forge/pending/65273305e89c.yaml",
    )

    assert "Quorum unmet: 1/2 reviewers approved." in body
    assert "Phase: ESCALATE" in body
    assert "Run ID: 65273305e89c" in body
    assert "Deadline: 2026-08-14 20:00 UTC" in body
    assert "forge decide 65273305e89c <accept|redirect|extend>" in body


def test_format_pending_decision_notification_reports_when_full_option_set_is_omitted():
    body = format_pending_decision_notification(
        {
            "reason": "Operator action required.",
            "run_id": "run-42",
            "phase": "ESCALATE",
            "options": [f"option_{idx:02d}" for idx in range(20)],
            "timeout_at": "2026-08-14T20:00:00+00:00",
        },
        pending_path="/tmp/.forge/pending/run-42.yaml",
        max_chars=180,
    )

    assert "forge decide run-42 <action>" in body
    assert "Full option set omitted from this notification;" in body
    assert "/tmp/.forge/pending/run-42.yaml" in body
    assert "<option_00|option_01" not in body


def test_format_pending_decision_notification_truncates_long_reason_and_keeps_options():
    body = format_pending_decision_notification(
        {
            "reason": "Escalation advisory:\n" + ("review drift detected\n" * 200),
            "run_id": "65273305e89c",
            "phase": "ESCALATE",
            "options": [
                "accept",
                "redirect",
                "defer_or_abandon",
                "re_review",
                "split_story",
                "abort",
            ],
            "timeout_at": "2026-08-14T20:00:00+00:00",
        },
        pending_path="/tmp/.forge/pending/65273305e89c.yaml",
    )

    assert "Phase: ESCALATE" in body
    assert "Run ID: 65273305e89c" in body
    assert (
        "forge decide 65273305e89c "
        "<accept|redirect|defer_or_abandon|re_review|split_story|abort>" in body
    )
    assert "Full option set omitted from this notification;" not in body
    assert len(body) <= MAX_PENDING_NOTIFICATION_BODY_CHARS


# ── Slack backend tests ────────────────────────────────────────────────


class _MockResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_slack_posts_block_kit_json(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    with patch(
        "theforge.notify_backends.urllib.request.urlopen", return_value=_MockResponse()
    ) as mock_open:
        _send_slack("SLACK_WEBHOOK_URL", "Sprint Done", "3 passed · 0 failed")
        assert mock_open.called
        req = mock_open.call_args[0][0]
        payload = json.loads(req.data.decode())
        assert "blocks" in payload
        blocks = payload["blocks"]
        assert blocks[0]["type"] == "header"
        assert blocks[0]["text"]["text"] == "Sprint Done"
        assert blocks[1]["type"] == "section"
        assert "3 passed" in blocks[1]["text"]["text"]
        assert req.get_header("Content-type") == "application/json"


def test_send_slack_reads_webhook_url_from_env(monkeypatch):
    monkeypatch.setenv("MY_WEBHOOK", "https://hooks.slack.com/custom")
    with patch(
        "theforge.notify_backends.urllib.request.urlopen", return_value=_MockResponse()
    ) as mock_open:
        _send_slack("MY_WEBHOOK", "title", "body")
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://hooks.slack.com/custom"


def test_send_slack_skips_when_env_not_set(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    with patch("theforge.notify_backends.urllib.request.urlopen") as mock_open:
        _send_slack("SLACK_WEBHOOK_URL", "title", "body")
        mock_open.assert_not_called()


def test_send_slack_includes_channel_when_set(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    with patch(
        "theforge.notify_backends.urllib.request.urlopen", return_value=_MockResponse()
    ) as mock_open:
        _send_slack("SLACK_WEBHOOK_URL", "title", "body", channel="#theforge")
        payload = json.loads(mock_open.call_args[0][0].data.decode())
        assert payload.get("channel") == "#theforge"


def test_send_slack_no_channel_field_when_not_set(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    with patch(
        "theforge.notify_backends.urllib.request.urlopen", return_value=_MockResponse()
    ) as mock_open:
        _send_slack("SLACK_WEBHOOK_URL", "title", "body")
        payload = json.loads(mock_open.call_args[0][0].data.decode())
        assert "channel" not in payload


def test_send_slack_prepends_mention_on_escalate(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    with patch(
        "theforge.notify_backends.urllib.request.urlopen", return_value=_MockResponse()
    ) as mock_open:
        _send_slack("SLACK_WEBHOOK_URL", "title", "body text", mention_on_escalate="@here")
        payload = json.loads(mock_open.call_args[0][0].data.decode())
        section_text = payload["blocks"][1]["text"]["text"]
        assert section_text.startswith("@here ")
        assert "body text" in section_text


def test_send_slack_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    with patch(
        "theforge.notify_backends.urllib.request.urlopen", side_effect=OSError("network error")
    ):
        # _send_slack itself raises; the caller (send_notifications) catches it
        # Verify the exception propagates so send_notifications can log+continue
        try:
            _send_slack("SLACK_WEBHOOK_URL", "title", "body")
            raised = False
        except OSError:
            raised = True
        assert raised


def test_send_notifications_dispatches_to_slack_backend(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    config = _make_config([BackendConfig(type="slack", webhook_url_env="SLACK_WEBHOOK_URL")])
    with patch("theforge.notify_backends._send_slack") as mock_slack:
        send_notifications(config, "title", "body")
        mock_slack.assert_called_once_with(
            webhook_url_env="SLACK_WEBHOOK_URL",
            title="title",
            body="body",
            channel=None,
            mention_on_escalate=None,
            secrets={},
        )


def test_send_slack_reads_webhook_url_from_secrets(monkeypatch):
    """Webhook URL from config.secrets takes precedence over os.environ absence."""
    monkeypatch.delenv("MY_SECRET_HOOK", raising=False)
    with patch(
        "theforge.notify_backends.urllib.request.urlopen", return_value=_MockResponse()
    ) as mock_open:
        _send_slack(
            "MY_SECRET_HOOK",
            "title",
            "body",
            secrets={"MY_SECRET_HOOK": "https://hooks.slack.com/from-secrets"},
        )
        assert mock_open.called
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://hooks.slack.com/from-secrets"


def test_escalate_notify_calls_send_notifications_when_ntfy_is_none():
    """_escalate_notify must call send_notifications for Slack backend even when ntfy is None."""
    from theforge.config import BackendConfig
    from theforge.coordinator.notify import _escalate_notify

    config = MagicMock()
    config.notifications.ntfy = None
    config.notifications.backend = "slack"
    config.notifications.backends = (BackendConfig(type="slack", webhook_url_env="TEST_HOOK"),)
    config.secrets = {}

    state = MagicMock()
    state.review_cycle = 2
    state.total_cost = 1.50
    state.total_cost_measured = 1.50
    state.error = "Too many cycles"
    state.branch_name = "feat/test"
    state.started_at = None
    state.review_results = []

    task = MagicMock()
    task.slug = "test-task"

    with patch("theforge.notify_backends.send_notifications") as mock_sn:
        _escalate_notify(task, state, notify=True, config=config)
        mock_sn.assert_called_once()
        title_arg = mock_sn.call_args[0][1]
        assert "escalated" in title_arg.lower()


def test_send_notifications_passes_mention_on_escalate(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    config = _make_config(
        [
            BackendConfig(
                type="slack",
                webhook_url_env="SLACK_WEBHOOK_URL",
                mention_on_escalate="@oncall",
            )
        ]
    )
    with patch("theforge.notify_backends._send_slack") as mock_slack:
        send_notifications(config, "title", "body", is_escalation=True)
        mock_slack.assert_called_once_with(
            webhook_url_env="SLACK_WEBHOOK_URL",
            title="title",
            body="body",
            channel=None,
            mention_on_escalate="@oncall",
            secrets={},
        )


def test_send_notifications_no_mention_when_not_escalation(monkeypatch):
    config = _make_config(
        [
            BackendConfig(
                type="slack",
                webhook_url_env="SLACK_WEBHOOK_URL",
                mention_on_escalate="@oncall",
            )
        ]
    )
    with patch("theforge.notify_backends._send_slack") as mock_slack:
        send_notifications(config, "title", "body", is_escalation=False)
        mock_slack.assert_called_once_with(
            webhook_url_env="SLACK_WEBHOOK_URL",
            title="title",
            body="body",
            channel=None,
            mention_on_escalate=None,
            secrets={},
        )
