"""Tests for HTTP timeout story (issue-292).

Verifies that all outbound HTTP calls from the coordinator have explicit
timeouts and that notification/PR failures are handled gracefully.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

# Module-level imports bypass the conftest autouse fixture that patches these
# symbols on the module object — local imports inside test methods get the mock.
from theforge.coordinator.ntfy_client import _ntfy_publish
from theforge.notify_backends import _send_slack, _send_webhook


class _MockResponse:
    """Minimal context-manager response stub for urllib urlopen mocks."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestNtfyPublishTimeout:
    """_ntfy_publish passes timeout= to urlopen and swallows failures."""

    def test_urlopen_called_with_timeout(self):
        """urlopen receives the timeout kwarg so the call cannot hang forever."""
        with patch(
            "theforge.coordinator.ntfy_client.urllib.request.urlopen",
            return_value=_MockResponse(),
        ) as mock_urlopen:
            _ntfy_publish("https://ntfy.sh/topic", "Test Title", "Test body")
            assert mock_urlopen.called
            _, kwargs = mock_urlopen.call_args
            assert "timeout" in kwargs, "urlopen must be called with a timeout= kwarg"
            timeout_val = kwargs["timeout"]
            assert 15 <= timeout_val <= 30, f"timeout {timeout_val!r} should be 15-30s"

    def test_network_error_does_not_raise(self):
        """A network failure is swallowed — _ntfy_publish never propagates exceptions."""
        with patch(
            "theforge.coordinator.ntfy_client.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            # Must not raise
            _ntfy_publish("https://ntfy.sh/topic", "Title", "Body")

    def test_timeout_error_does_not_raise(self):
        """A socket timeout is swallowed — _ntfy_publish never propagates exceptions."""
        import socket

        with patch(
            "theforge.coordinator.ntfy_client.urllib.request.urlopen",
            side_effect=socket.timeout("timed out"),
        ):
            _ntfy_publish("https://ntfy.sh/topic", "Title", "Body")

    def test_failure_logs_warning(self, caplog):
        """A publish failure logs a WARNING message via Python logging."""
        import logging

        with caplog.at_level(logging.WARNING, logger="theforge.coordinator.ntfy_client"):
            with patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen",
                side_effect=OSError("network unreachable"),
            ):
                _ntfy_publish("https://ntfy.sh/topic", "Title", "Body")

        assert any(r.levelno == logging.WARNING for r in caplog.records), (
            "failure must be logged at WARNING level"
        )
        assert any("ntfy" in r.message.lower() for r in caplog.records), (
            "log message should mention ntfy"
        )


class TestNtfyPollTimeouts:
    """Poll helpers also pass timeout= to urlopen."""

    def test_poll_reply_urlopen_has_timeout(self):
        from theforge.coordinator.ntfy_client import _ntfy_poll_reply

        resp = _MockResponse()
        resp.read = lambda: b'{"event":"message","message":"approve"}\n'

        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch(
                "theforge.coordinator.ntfy_client.time.monotonic",
                side_effect=[0.0, 0.0, 0.0, 0.0, 100.0],
            ),
        ):
            _ntfy_poll_reply(reply_url="https://ntfy.sh/r", since_ts=0, timeout_seconds=30)
            assert mock_urlopen.called
            _, call_kwargs = mock_urlopen.call_args
            assert "timeout" in call_kwargs, "poll urlopen must have timeout="

    def test_poll_plan_reply_urlopen_has_timeout(self):
        from theforge.coordinator.ntfy_client import _ntfy_poll_plan_reply

        resp = _MockResponse()
        resp.read = lambda: b'{"event":"message","message":"approve"}\n'

        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch(
                "theforge.coordinator.ntfy_client.time.monotonic",
                side_effect=[0.0, 0.0, 0.0, 100.0],
            ),
        ):
            _ntfy_poll_plan_reply("https://ntfy.sh/r", 0, 30)
            assert mock_urlopen.called
            _, call_kwargs = mock_urlopen.call_args
            assert "timeout" in call_kwargs

    def test_poll_escalate_reply_urlopen_has_timeout(self):
        from theforge.coordinator.ntfy_client import _ntfy_poll_escalate_reply

        resp = _MockResponse()
        resp.read = lambda: b'{"event":"message","message":"approve"}\n'

        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch(
                "theforge.coordinator.ntfy_client.time.monotonic",
                side_effect=[0.0, 0.0, 0.0, 100.0],
            ),
        ):
            _ntfy_poll_escalate_reply("https://ntfy.sh/r", 0, 30)
            assert mock_urlopen.called
            _, call_kwargs = mock_urlopen.call_args
            assert "timeout" in call_kwargs


def _make_parsed_review():
    """Build a minimal APPROVE ReviewResult for use in completion tests."""
    from theforge.review import ReviewResult

    return ReviewResult(
        verdict="APPROVE",
        summary="Looks good.",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


class TestGhSubprocessTimeout:
    """subprocess calls to `gh` and `git` in completion.py have a timeout."""

    def _fake_run_factory(self, *, gh_create_raises=None, gh_create_rc=0):
        """Return a fake subprocess.run that records calls and simulates success."""
        captured: list[dict] = []

        def fake_run(cmd, **kwargs):
            captured.append({"cmd": cmd, "kwargs": kwargs})
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if cmd[0] == "git" and "rev-list" in cmd:
                result.stdout = "1\n"
            elif cmd[0] == "gh" and "list" in cmd:
                result.stdout = "[]"
            elif cmd[0] == "gh" and "create" in cmd:
                if gh_create_raises:
                    raise gh_create_raises
                result.returncode = gh_create_rc
                result.stdout = "https://github.com/owner/repo/pull/1" if gh_create_rc == 0 else ""
                result.stderr = "server error" if gh_create_rc != 0 else ""
            else:
                result.stdout = ""
            return result

        return fake_run, captured

    def test_all_gh_calls_have_timeout(self, tmp_path):
        """Every subprocess call to `gh` includes a timeout= kwarg."""
        from coord_test_helpers import _make_config, _make_task

        from theforge.coordinator.completion import _create_pr
        from theforge.coordinator.state import CoordinatorState

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        parsed_review = _make_parsed_review()

        fake_run, captured = self._fake_run_factory()
        worktree = tmp_path / task.slug
        worktree.mkdir(parents=True)

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run):
            _create_pr(config, task, "feat/test-branch", parsed_review, state)

        gh_calls = [c for c in captured if c["cmd"][0] == "gh"]
        assert gh_calls, "expected at least one gh subprocess call"
        for c in gh_calls:
            assert "timeout" in c["kwargs"], f"gh call {c['cmd']} missing timeout=: {c['kwargs']}"

    def test_gh_pr_create_failure_is_non_fatal(self, tmp_path):
        """gh pr create failure returns success=False but does not raise."""
        from coord_test_helpers import _make_config, _make_task

        from theforge.coordinator.completion import _create_pr
        from theforge.coordinator.state import CoordinatorState

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        parsed_review = _make_parsed_review()

        fake_run, _ = self._fake_run_factory(gh_create_rc=1)
        worktree = tmp_path / task.slug
        worktree.mkdir(parents=True)

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run):
            result = _create_pr(config, task, "feat/test-branch", parsed_review, state)

        assert result["success"] is False
        assert result["pr_url"] is None
        assert result.get("error")

    def test_gh_timeout_exception_is_non_fatal(self, tmp_path):
        """TimeoutExpired from gh pr create is caught — returns success=False, never raises."""
        from coord_test_helpers import _make_config, _make_task

        from theforge.coordinator.completion import _create_pr
        from theforge.coordinator.state import CoordinatorState

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        parsed_review = _make_parsed_review()

        fake_run, _ = self._fake_run_factory(
            gh_create_raises=subprocess.TimeoutExpired(["gh", "pr", "create"], timeout=60)
        )
        worktree = tmp_path / task.slug
        worktree.mkdir(parents=True)

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run):
            result = _create_pr(config, task, "feat/test-branch", parsed_review, state)

        assert result["success"] is False


class TestNotifyBackendsTimeout:
    """notify_backends urlopen calls also have explicit timeouts."""

    def test_webhook_urlopen_has_timeout(self):
        """_send_webhook passes timeout= to urlopen."""
        with patch(
            "theforge.notify_backends.urllib.request.urlopen",
            return_value=_MockResponse(),
        ) as mock_urlopen:
            _send_webhook("https://example.com/hook", "Title", "Body")
            assert mock_urlopen.called
            _, kwargs = mock_urlopen.call_args
            assert "timeout" in kwargs, "webhook urlopen must have timeout="

    def test_slack_urlopen_has_timeout(self, monkeypatch):
        """_send_slack passes timeout= to urlopen."""
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        with patch(
            "theforge.notify_backends.urllib.request.urlopen",
            return_value=_MockResponse(),
        ) as mock_urlopen:
            _send_slack(
                webhook_url_env="SLACK_WEBHOOK_URL",
                title="Title",
                body="Body",
            )
            assert mock_urlopen.called
            _, kwargs = mock_urlopen.call_args
            assert "timeout" in kwargs, "Slack urlopen must have timeout="

    def test_backend_failure_does_not_propagate(self):
        """A urlopen failure in a backend is caught by send_notifications — never raises."""
        from theforge.notify_backends import send_notifications

        config = MagicMock()
        config.notifications.backends = [MagicMock(type="webhook", url="https://example.com/hook")]
        config.secrets = {}

        with patch(
            "theforge.notify_backends._send_webhook",
            side_effect=OSError("connection timeout"),
        ):
            # Must not raise
            send_notifications(config, "title", "body")
