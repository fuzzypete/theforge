"""Tests for human-review and ntfy publish/poll notification behaviour.

Classes:
- TestCoordinatorHumanReview — interactive HUMAN_REVIEW phase (R7)
- TestRemoteHumanReview — remote async HITL via ntfy action buttons
- TestNtfyPublish — unit tests for _ntfy_publish()
- TestNtfyPollReply — unit tests for _ntfy_poll_reply()
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED,
    REQUEST_CHANGES_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_ntfy_config,
    _make_pool_result,
    _make_task,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    NotificationConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.notify import _is_remote_mode
from theforge.coordinator.ntfy_client import (
    _ntfy_poll_reply,
    _ntfy_publish,
    _ntfy_reply_url,
)
from theforge.coordinator.state import Phase


class TestCoordinatorHumanReview:
    """Tests for the HUMAN_REVIEW phase (R7 from the spec)."""

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _make_interactive_base(tmp_path):
        """Return (config, task, workspace) with workspace already created."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        return config, task, workspace

    @staticmethod
    def _shell_side_effect(workspace):
        """Standard shell mock: gate writes PASS handoff, git status is clean."""

        def side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(workspace, "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            return (True, "OK")

        return side_effect

    # ── test_interactive_approve ──────────────────────────────────────

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_approve(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Human enters 'a' → DONE."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("sys.stdin", io.StringIO("a\n")):
            result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.human_review_decision == "approve"
        assert result.state.human_review_feedback is None

    # ── test_interactive_reject_loops_back ────────────────────────────

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_reject_loops_back(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Human enters 'r' + findings → dev called again with human_feedback, then approves."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")

        # First review cycle: APPROVE → human rejects; second cycle: APPROVE → human approves
        approve_result = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_pool.return_value = approve_result  # same list each call (pool is called twice)

        # Use side_effect list so first call triggers reject path, second triggers approve
        stdin_input = "r\nfix the bug\n\na\n"
        with patch("sys.stdin", io.StringIO(stdin_input)):
            result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        # dev agent was called at least twice (original + after rejection)
        assert len(result.state.dev_results) >= 2
        # The human_review_decision records the final decision
        assert result.state.human_review_decision == "approve"

    # ── test_interactive_escalate ─────────────────────────────────────

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_escalate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Human enters 'e' → ESCALATE."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("sys.stdin", io.StringIO("e\n")):
            result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "escalate"

    # ── test_auto_mode_skips_human_review ─────────────────────────────

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_mode_skips_human_review(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """interactive=False never enters HUMAN_REVIEW."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=False)

        assert result.success is True
        assert result.phase == Phase.DONE
        # HUMAN_REVIEW phase was never set
        assert result.state.human_review_decision is None
        # The phase stored in state at completion is DONE (not HUMAN_REVIEW)
        assert result.state.phase == Phase.DONE

    # ── test_interactive_on_exhausted_cycles ─────────────────────────

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_on_exhausted_cycles(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """When review cycles exhaust with REQUEST_CHANGES, human can still choose."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        # Always REQUEST_CHANGES → cycles exhaust → HUMAN_REVIEW
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        # Human escalates at the HUMAN_REVIEW prompt
        with patch("sys.stdin", io.StringIO("e\n")):
            result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "escalate"


class TestRemoteHumanReview:
    """Remote async HITL via ntfy action buttons."""

    def test_ntfy_reply_url(self):
        assert _ntfy_reply_url("https://ntfy.sh/my-topic") == "https://ntfy.sh/my-topic-reply"
        assert _ntfy_reply_url("https://ntfy.sh/my-topic/") == "https://ntfy.sh/my-topic-reply"

    def test_remote_mode_not_activated_without_notify(self, tmp_path):
        """notify=False → remote mode is off even with ntfy configured."""
        config = _make_ntfy_config(tmp_path)
        assert not _is_remote_mode(False, config)


class TestNtfyPublish:
    def test_ntfy_publish_sanitizes_unicode_title_header(self):
        """Forge-style Unicode titles are normalized before hitting urllib headers."""
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["title"] = req.headers["Title"]
            captured["priority"] = req.headers["Priority"]
            captured["content_type"] = req.headers.get("Content-Type") or req.headers.get(
                "Content-type"
            )
            captured["body"] = req.data.decode("utf-8")
            return _Resp()

        with patch(
            "theforge.coordinator.ntfy_client.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            _ntfy_publish(
                "https://ntfy.sh/example-topic",
                "TheForge: ✓ done — demo",
                "body",
                priority="high",
            )

        assert captured["url"] == "https://ntfy.sh/example-topic"
        assert captured["timeout"] == 15
        assert captured["title"] == "TheForge: OK done - demo"
        assert captured["priority"] == "high"
        assert captured["content_type"] == "text/plain; charset=utf-8"
        assert captured["body"] == "body"

    def test_remote_mode_not_activated_without_ntfy(self, tmp_path):
        """Non-ntfy backend → remote mode is off."""
        config = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(),
            notifications=NotificationConfig(backend="none"),
        )
        assert not _is_remote_mode(True, config)

    def test_remote_mode_activated_with_ntfy(self, tmp_path):
        """notify=True + ntfy backend + NtfyConfig → remote mode is on."""
        config = _make_ntfy_config(tmp_path)
        assert _is_remote_mode(True, config)

    def test_remote_approve(self, tmp_path):
        """ntfy poll returns 'approve' → task reaches DONE."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                return_value=_make_agent_result(output="Done."),
            ),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.remote_gates._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_reply",
                return_value=("approve", None),
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        assert result.success
        assert result.phase == Phase.DONE
        assert result.state.human_review_decision == "approve"
        assert result.state.human_review_mode == "remote"

    def test_remote_escalate(self, tmp_path):
        """ntfy poll returns 'escalate' → task reaches ESCALATE."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                return_value=_make_agent_result(output="Done."),
            ),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.remote_gates._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_reply",
                return_value=("escalate", None),
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        assert not result.success
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "escalate"

    def test_remote_timeout(self, tmp_path):
        """ntfy poll times out → auto-escalate + timeout notification."""
        config = _make_ntfy_config(tmp_path, timeout=60)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        ntfy_calls: list[tuple] = []

        def capture_ntfy(url, title, body, **kwargs):
            ntfy_calls.append((url, title, body))

        with (
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                return_value=_make_agent_result(output="Done."),
            ),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.remote_gates._ntfy_publish", side_effect=capture_ntfy),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_reply",
                return_value=("timeout", None),
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        assert not result.success
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "timeout"
        # Timeout notification should have been sent
        timeout_notifs = [c for c in ntfy_calls if "timed out" in c[1].lower()]
        assert len(timeout_notifs) >= 1

    def test_remote_reject_with_findings(self, tmp_path):
        """ntfy poll returns reject with findings → findings fed back to dev, then approve."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dev_prompts: list[str] = []

        def dev_side_effect(**kwargs):
            dev_prompts.append(kwargs.get("prompt", ""))
            return _make_agent_result(output="Done.")

        approve_result = _make_pool_result([APPROVE_REVIEW], ["review"])

        poll_calls: list[int] = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            poll_calls.append(1)
            if len(poll_calls) == 1:
                return ("reject", "fix the error handling")
            return ("approve", None)  # second human review approves

        with (
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=dev_side_effect),
            patch("theforge.coordinator.review_pool.run_agent_pool", return_value=approve_result),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.remote_gates._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_reply", side_effect=poll_side_effect
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        # Flow: preflight → dev1 → review(APPROVE) → human(reject)
        #       → dev2 → review(APPROVE) → human(approve) → DONE
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.human_review_decision == "approve"
        # dev ran at least 2 times: dev1 + dev-after-reject (preflight mocked separately)
        assert len(dev_prompts) >= 2
        # Rejection text "fix the error handling" must appear in the post-reject dev prompt
        post_reject_prompts = " ".join(dev_prompts[1:])
        assert "fix the error handling" in post_reject_prompts

    def test_remote_extend_grants_cycle(self, tmp_path):
        """ntfy poll returns 'extend' → fresh dev+review budget granted."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        poll_calls = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            poll_calls.append(1)
            if len(poll_calls) == 1:
                return ("extend", None)
            return ("approve", None)

        dev_calls = []

        def dev_side_effect(**kwargs):
            dev_calls.append(1)
            if len(dev_calls) == 1:
                return _make_agent_result(output=PREFLIGHT_PROCEED, cost_usd=0.05)
            return _make_agent_result(output="Done.")

        with (
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=dev_side_effect),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.remote_gates._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_reply", side_effect=poll_side_effect
            ),
        ):
            result = run_task(config, task, interactive=True, notify=True)

        # extend → extra_cycles incremented
        assert result.state.human_review_extra_cycles >= 1
        assert result.state.human_review_mode == "remote"


class TestNtfyPollReply:
    """Unit tests for _ntfy_poll_reply() — mock urlopen/time to avoid real I/O."""

    def _make_resp(self, lines: list[str]):
        """Return a fake context-manager response whose read() returns the given lines."""
        content = "\n".join(lines).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = content
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_poll_returns_approve_immediately(self):
        """Single 'approve' message in response → returns ('approve', None)."""
        resp = self._make_resp(['{"event":"message","message":"approve"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("approve", None)

    def test_poll_returns_extend(self):
        """'extend' message → returns ('extend', None)."""
        resp = self._make_resp(['{"event":"message","message":"extend"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("extend", None)

    def test_poll_returns_escalate(self):
        """'escalate' message → returns ('escalate', None)."""
        resp = self._make_resp(['{"event":"message","message":"escalate"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("escalate", None)

    def test_poll_reject_with_findings(self):
        """'reject: fix the bug' → returns ('reject', 'fix the bug')."""
        resp = self._make_resp(['{"event":"message","message":"reject: fix the bug"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("reject", "fix the bug")

    def test_poll_reject_empty_findings(self):
        """'reject:' with no trailing text → findings is None."""
        resp = self._make_resp(['{"event":"message","message":"reject:"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("reject", None)

    def test_poll_uses_since_parameter(self):
        """Verify the URL contains poll=1&since=<ts>."""
        captured_urls: list[str] = []

        resp = self._make_resp(['{"event":"message","message":"approve"}'])

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return resp

        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen", side_effect=fake_urlopen
            ),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)

        assert len(captured_urls) >= 1
        assert "poll=1" in captured_urls[0]
        assert "since=1700000000" in captured_urls[0]

    def test_poll_ignores_unknown_messages(self):
        """Unknown message on first response, valid on second → returns valid decision."""
        resp1 = self._make_resp(['{"event":"message","message":"unknown-action"}'])
        resp2 = self._make_resp(['{"event":"message","message":"approve"}'])
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            return resp1 if call_count == 1 else resp2

        # deadline=60s; first poll at t=0 < 60; sleep; second poll at t=1 < 60; returns
        monotonic_vals = iter([0.0, 0.0, 1.0, 1.0, 1.0])
        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen", side_effect=fake_urlopen
            ),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)

        assert result == ("approve", None)
        assert call_count == 2

    def test_poll_timeout_when_no_reply(self):
        """monotonic advances past deadline → returns ('timeout', None)."""
        # call 1: deadline = monotonic() + 60 → deadline=60
        # call 2: while monotonic() < 60 → True, enter loop
        # urlopen raises → sleep calc: call 3 (returns 10, so sleep(10))
        # call 4: while monotonic() < 60 → 61 >= 60 → exit loop
        monotonic_vals = iter([0.0, 0.0, 10.0, 61.0])
        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen",
                side_effect=Exception("no data"),
            ),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("timeout", None)

    def test_poll_sleeps_10_seconds_between_polls(self):
        """time.sleep is called with ~10 seconds when deadline is far away."""
        resp_empty = self._make_resp([""])
        resp_approve = self._make_resp(['{"event":"message","message":"approve"}'])
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            return resp_empty if call_count == 1 else resp_approve

        sleep_args: list[float] = []

        # t=0 (deadline check), t=0 (after failed parse, compute sleep), t=1 (loop check), t=1, t=1
        monotonic_vals = iter([0.0, 0.0, 1.0, 1.0, 1.0])
        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen", side_effect=fake_urlopen
            ),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch(
                "theforge.coordinator.ntfy_client.time.sleep",
                side_effect=lambda s: sleep_args.append(s),
            ),
        ):
            _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)

        assert len(sleep_args) >= 1
        # sleep should be capped at 10s; with deadline=60 and t=0, remaining=60 → sleep=10
        assert sleep_args[0] == pytest.approx(10.0)

    def test_poll_skips_non_message_events(self):
        """ntfy keepalive/open events (event != 'message') are ignored."""
        resp = self._make_resp(
            [
                '{"event":"open","message":""}',
                '{"event":"keepalive","message":""}',
                '{"event":"message","message":"approve"}',
            ]
        )
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == ("approve", None)
