"""Tests for notification-related coordinator behaviour.

Extracted from test_coordinator.py:
- TestCoordinatorHumanReview — interactive HUMAN_REVIEW phase (R7)
- TestRemoteHumanReview — remote async HITL via ntfy action buttons
- TestNtfyPollReply — unit tests for _ntfy_poll_reply()
- TestNtfyPollPlanReply — unit tests for _ntfy_poll_plan_reply()
- TestPlanReviewRemote — unit tests for _plan_review_remote()
- TestNtfyTerminalNotifications — ntfy publish at DONE / ESCALATE
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_ALREADY_DONE,
    PREFLIGHT_PROCEED,
    REQUEST_CHANGES_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_ntfy_config,
    _make_pool_result,
    _make_task,
    _preflight_then,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    NotificationConfig,
    NtfyConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import Phase, _is_remote_mode, run_task
from theforge.coordinator.ntfy_client import (
    _ntfy_poll_plan_reply,
    _ntfy_poll_reply,
    _ntfy_publish,
    _ntfy_reply_url,
)
from theforge.coordinator.remote_gates import _plan_review_remote
from theforge.coordinator.state import CoordinatorState


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

    @patch("theforge.coordinator.engine.run_agent_pool")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Human enters 'a' → DONE."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
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

    @patch("theforge.coordinator.engine.run_agent_pool")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_reject_loops_back(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Human enters 'r' + findings → dev called again with human_feedback, then approves."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))

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

    @patch("theforge.coordinator.engine.run_agent_pool")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_escalate(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Human enters 'e' → ESCALATE."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("sys.stdin", io.StringIO("e\n")):
            result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.human_review_decision == "escalate"

    # ── test_auto_mode_skips_human_review ─────────────────────────────

    @patch("theforge.coordinator.engine.run_agent_pool")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_mode_skips_human_review(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """interactive=False never enters HUMAN_REVIEW."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
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

    @patch("theforge.coordinator.engine.run_agent_pool")
    @patch("theforge.coordinator.engine.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_interactive_on_exhausted_cycles(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """When review cycles exhaust with REQUEST_CHANGES, human can still choose."""
        config, task, workspace = self._make_interactive_base(tmp_path)
        mock_shell.side_effect = self._shell_side_effect(workspace)
        mock_agent.side_effect = _preflight_then(_make_agent_result(success=True, output="Done."))
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
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
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
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
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
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
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
            # call 1: preflight; call 2: dev (first run); call 3: dev after reject
            if len(dev_prompts) == 1:
                return _make_agent_result(output=PREFLIGHT_PROCEED, cost_usd=0.05)
            return _make_agent_result(output="Done.")

        approve_result = _make_pool_result([APPROVE_REVIEW], ["review"])

        poll_calls: list[int] = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            poll_calls.append(1)
            if len(poll_calls) == 1:
                return ("reject", "fix the error handling")
            return ("approve", None)  # second human review approves

        with (
            patch("theforge.coordinator.engine.run_agent", side_effect=dev_side_effect),
            patch("theforge.coordinator.engine.run_agent_pool", return_value=approve_result),
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
        # dev ran at least 3 times: preflight + dev1 + dev-after-reject
        assert len(dev_prompts) >= 3
        # Rejection text "fix the error handling" must appear in the post-reject dev prompt
        post_reject_prompts = " ".join(dev_prompts[2:])
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
            patch("theforge.coordinator.engine.run_agent", side_effect=dev_side_effect),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
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


class TestNtfyPollPlanReply:
    """Unit tests for _ntfy_poll_plan_reply() — plan review decision polling."""

    def _make_resp(self, lines: list[str]):
        content = "\n".join(lines).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = content
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_poll_returns_approve(self):
        """'approve' message → returns 'approve'."""
        resp = self._make_resp(['{"event":"message","message":"approve"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_plan_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == "approve"

    def test_poll_returns_regenerate(self):
        """'regenerate' message → returns 'regenerate'."""
        resp = self._make_resp(['{"event":"message","message":"regenerate"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_plan_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == "regenerate"

    def test_poll_returns_abandon(self):
        """'abandon' message → returns 'abandon'."""
        resp = self._make_resp(['{"event":"message","message":"abandon"}'])
        monotonic_vals = iter([0.0, 0.0, 0.0])
        with (
            patch("theforge.coordinator.ntfy_client.urllib.request.urlopen", return_value=resp),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_plan_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == "abandon"

    def test_poll_timeout(self):
        """Deadline exceeded → returns 'timeout'."""
        monotonic_vals = iter([0.0, 0.0, 10.0, 61.0])
        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen",
                side_effect=Exception("no data"),
            ),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_plan_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == "timeout"

    def test_poll_ignores_unknown_messages(self):
        """Unknown message on first response, valid 'abandon' on second → returns 'abandon'."""
        resp1 = self._make_resp(['{"event":"message","message":"unknown"}'])
        resp2 = self._make_resp(['{"event":"message","message":"abandon"}'])
        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            return resp1 if call_count == 1 else resp2

        monotonic_vals = iter([0.0, 0.0, 1.0, 1.0, 1.0])
        with (
            patch(
                "theforge.coordinator.ntfy_client.urllib.request.urlopen", side_effect=fake_urlopen
            ),
            patch("theforge.coordinator.ntfy_client.time.monotonic", side_effect=monotonic_vals),
            patch("theforge.coordinator.ntfy_client.time.sleep"),
        ):
            result = _ntfy_poll_plan_reply("https://ntfy.sh/reply-topic", 1700000000, 60)
        assert result == "abandon"
        assert call_count == 2


def _make_ntfy_plan_review_cfg(
    tmp_path,
    *,
    mode: str = "blocking",
    timeout_seconds: int = 10,
) -> ForgeConfig:
    return ForgeConfig(
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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        notifications=NotificationConfig(
            backend="ntfy",
            ntfy=NtfyConfig(url="https://ntfy.sh/test-topic", priority="default"),
        ),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
        plan_review=PlanReviewConfig(enabled=True, mode=mode, timeout_seconds=timeout_seconds),
        log=LogConfig(enabled=False),
    )


class TestPlanReviewRemote:
    """Unit tests for _plan_review_remote() — ntfy-backed plan review."""

    def test_remote_approve(self, tmp_path):
        """ntfy reply 'approve' → returns 'approve', sets mode to 'remote'."""
        config = _make_ntfy_plan_review_cfg(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = CoordinatorState()

        with (
            patch("theforge.coordinator.notify._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_plan_reply",
                return_value="approve",
            ),
            patch("theforge.coordinator.remote_gates.time.time", return_value=1700000000),
        ):
            result = _plan_review_remote(state, "# Plan\n\nDetails.", workspace, task, config)

        assert result == "approve"
        assert state.plan_review_mode == "remote"

    def test_remote_regenerate(self, tmp_path):
        """ntfy reply 'regenerate' → returns 'regenerate'."""
        config = _make_ntfy_plan_review_cfg(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = CoordinatorState()

        with (
            patch("theforge.coordinator.notify._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_plan_reply",
                return_value="regenerate",
            ),
            patch("theforge.coordinator.remote_gates.time.time", return_value=1700000000),
        ):
            result = _plan_review_remote(state, "# Plan", workspace, task, config)

        assert result == "regenerate"

    def test_remote_blocking_retries_on_timeout_until_decision(self, tmp_path):
        """Blocking mode + timeout → keeps polling; returns first explicit decision."""
        config = _make_ntfy_plan_review_cfg(tmp_path, mode="blocking")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = CoordinatorState()

        poll_calls: list[int] = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            poll_calls.append(1)
            # First two polls timeout; third returns abandon
            if len(poll_calls) < 3:
                return "timeout"
            return "abandon"

        with (
            patch("theforge.coordinator.notify._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_plan_reply",
                side_effect=poll_side_effect,
            ),
            patch("theforge.coordinator.remote_gates.time.time", return_value=1700000000),
        ):
            result = _plan_review_remote(state, "# Plan", workspace, task, config)

        assert result == "abandon"
        assert state.plan_review_mode == "remote"
        assert len(poll_calls) == 3  # polled 3 times before getting a decision

    def test_remote_blocking_cursor_preserved_across_chunks(self, tmp_path):
        """Blocking mode preserves the initial since_ts across all poll iterations.

        If the cursor were reset to time.time() after each chunk, a reply arriving
        just before the reset would be skipped permanently. Verify all poll calls
        receive the same original since_ts value.
        """
        config = _make_ntfy_plan_review_cfg(tmp_path, mode="blocking")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = CoordinatorState()

        initial_ts = 1700000000
        seen_cursors: list[int] = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            seen_cursors.append(since_ts)
            return "timeout" if len(seen_cursors) < 3 else "approve"

        with (
            patch("theforge.coordinator.notify._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_plan_reply",
                side_effect=poll_side_effect,
            ),
            patch("theforge.coordinator.remote_gates.time.time", return_value=initial_ts),
        ):
            result = _plan_review_remote(state, "# Plan", workspace, task, config)

        assert result == "approve"
        assert len(seen_cursors) == 3
        # All iterations must use the same original cursor, not a reset one
        assert all(c == initial_ts for c in seen_cursors), (
            f"Cursor was reset between chunks: {seen_cursors}"
        )

    def test_remote_blocking_does_not_auto_abandon_on_single_timeout(self, tmp_path):
        """Blocking mode never auto-abandons: a single timeout chunk re-polls."""
        config = _make_ntfy_plan_review_cfg(tmp_path, mode="blocking")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = CoordinatorState()

        poll_calls: list[int] = []

        def poll_side_effect(reply_url, since_ts, timeout_seconds):
            poll_calls.append(1)
            return "timeout" if len(poll_calls) == 1 else "approve"

        with (
            patch("theforge.coordinator.notify._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_plan_reply",
                side_effect=poll_side_effect,
            ),
            patch("theforge.coordinator.remote_gates.time.time", return_value=1700000000),
        ):
            result = _plan_review_remote(state, "# Plan", workspace, task, config)

        assert result == "approve"
        assert len(poll_calls) == 2

    def test_remote_advisory_timeout_approves(self, tmp_path):
        """Advisory mode + timeout → auto-approves, sets mode to 'advisory-timeout'."""
        config = _make_ntfy_plan_review_cfg(tmp_path, mode="advisory")
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = CoordinatorState()

        with (
            patch("theforge.coordinator.notify._ntfy_publish"),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_plan_reply",
                return_value="timeout",
            ),
            patch("theforge.coordinator.remote_gates.time.time", return_value=1700000000),
        ):
            result = _plan_review_remote(state, "# Plan", workspace, task, config)

        assert result == "approve"
        assert state.plan_review_mode == "advisory-timeout"

    def test_remote_ntfy_payload_format(self, tmp_path):
        """ntfy publish uses 'TheForge: plan ready — <slug>' title + first 3 lines."""
        config = _make_ntfy_plan_review_cfg(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = CoordinatorState()

        plan_text = "# Plan\n\nLine 2.\nLine 3.\nLine 4 (should be excluded)."
        publish_calls: list[dict] = []

        def capture_publish(url, title, body, **kwargs):
            publish_calls.append({"url": url, "title": title, "body": body, **kwargs})

        with (
            patch("theforge.coordinator.remote_gates._ntfy_publish", side_effect=capture_publish),
            patch(
                "theforge.coordinator.remote_gates._ntfy_poll_plan_reply", return_value="approve"
            ),
            patch("theforge.coordinator.remote_gates.time.time", return_value=1700000000),
        ):
            _plan_review_remote(state, plan_text, workspace, task, config)

        # First publish call is the plan review notification
        assert len(publish_calls) >= 1
        notif = publish_calls[0]
        assert notif["title"] == f"TheForge: plan ready \u2014 {task.slug}"
        # Body includes first 3 lines
        assert "# Plan" in notif["body"]
        assert "Line 4" not in notif["body"]
        # Body includes worktree path
        assert f"Worktree: .forge/worktrees/{task.slug}" in notif["body"]
        # No 'edit' action exposed remotely
        assert "edit" not in notif.get("actions", "").lower()
        # Actions contain approve, regenerate, abandon
        assert "approve" in notif.get("actions", "").lower()
        assert "regenerate" in notif.get("actions", "").lower()
        assert "abandon" in notif.get("actions", "").lower()


class TestNtfyTerminalNotifications:
    """ntfy publish calls at DONE and ESCALATE terminal states."""

    def test_done_publishes_ntfy(self, tmp_path):
        """DONE state sends an ntfy notification with correct title and body."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
        ):
            result = run_task(config, task, notify=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert mock_ntfy.called
        # Find the DONE notification (title contains "✓ done")
        done_calls = [c for c in mock_ntfy.call_args_list if "done" in c.args[1]]
        assert len(done_calls) == 1, f"Expected 1 DONE ntfy call, got: {mock_ntfy.call_args_list}"
        call = done_calls[0]
        title = call.args[1]
        body = call.args[2]
        assert "✓" in title
        assert "done" in title
        assert "test-task" in title
        assert "APPROVE" in body
        assert "$" in body  # cost present
        assert "Branch:" in body
        assert "Looks good." in body  # parsed_review.summary from APPROVE_REVIEW
        # No action buttons on DONE notifications
        assert "actions" not in call.kwargs

    def test_done_summary_truncated(self, tmp_path):
        """Long review summary is truncated to 120 chars in DONE body."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        long_summary = "A" * 200
        long_approve = f"""\
```yaml
verdict: APPROVE
summary: "{long_summary}"
findings: []
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""
        with (
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
                return_value=_make_pool_result([long_approve], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
        ):
            result = run_task(config, task, notify=True)

        assert result.success is True
        done_calls = [c for c in mock_ntfy.call_args_list if "done" in c.args[1]]
        assert len(done_calls) == 1
        body = done_calls[0].args[2]
        body_lines = body.splitlines()
        # The summary line (line 2) must be at most 120 chars
        assert len(body_lines[1]) <= 120

    def test_escalate_publishes_ntfy(self, tmp_path):
        """ESCALATE state sends an ntfy notification with correct title and body."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(
                    _make_agent_result(output="Done."),
                    _make_agent_result(output="Fixed."),
                ),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
                return_value=_make_pool_result([REQUEST_CHANGES_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
        ):
            # max_review_cycles=2 in _make_ntfy_config; run until exhausted (no interactive)
            result = run_task(config, task, notify=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        escalate_calls = [c for c in mock_ntfy.call_args_list if "escalated" in c.args[1]]
        assert len(escalate_calls) >= 1, (
            f"Expected ntfy ESCALATE call, got: {mock_ntfy.call_args_list}"
        )
        call = escalate_calls[-1]  # last call is the cycles-exhausted one
        title = call.args[1]
        body = call.args[2]
        body_lines = body.splitlines()
        assert "✗" in title
        assert "escalated" in title
        assert "test-task" in title
        assert "cycles exhausted" in body_lines[0]  # always uses cycles format
        assert "$" in body_lines[0]  # cost present in first line
        # Second line: P1 description (or error), truncated to 120 chars
        assert "Off by one" in body_lines[1]  # P1 description from REQUEST_CHANGES_REVIEW
        assert len(body_lines[1]) <= 120
        assert "Branch:" in body
        # No action buttons on ESCALATE notifications
        assert "actions" not in call.kwargs

    def test_escalate_detail_truncated(self, tmp_path):
        """Long P1 description is truncated to 120 chars in ESCALATE body."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        long_desc = "X" * 200
        long_p1_review = f"""\
```yaml
verdict: REQUEST_CHANGES
summary: "Bug found."
findings:
  - severity: P1
    file: src/foo.py
    line: 10
    description: "{long_desc}"
    suggestion: "Fix it"
story_compliance:
  matches_spec: false
  mismatches: []
test_coverage:
  adequate: false
  gaps: []
```
"""
        with (
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(
                    _make_agent_result(output="Done."),
                    _make_agent_result(output="Fixed."),
                ),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
                return_value=_make_pool_result([long_p1_review], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
        ):
            result = run_task(config, task, notify=True)

        assert result.phase == Phase.ESCALATE
        escalate_calls = [c for c in mock_ntfy.call_args_list if "escalated" in c.args[1]]
        assert len(escalate_calls) >= 1
        body = escalate_calls[-1].args[2]
        detail_line = body.splitlines()[1]
        assert len(detail_line) <= 120
        assert detail_line == "X" * 120

    def test_escalate_non_cycle_body_uses_cycle_format(self, tmp_path):
        """Non-cycle ESCALATE (workspace failure) still uses '{cycles} cycles exhausted' format."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)

        with (
            patch(
                "theforge.coordinator.util._run_shell",
                return_value=(False, "git error"),
            ),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
        ):
            result = run_task(config, task, notify=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        escalate_calls = [c for c in mock_ntfy.call_args_list if "escalated" in c.args[1]]
        assert len(escalate_calls) >= 1
        body = escalate_calls[0].args[2]
        # Spec format always uses cycles exhausted (review_cycle=0 for workspace failure)
        assert "0 cycles exhausted" in body
        assert "$" in body
        assert "Branch:" in body

    def test_no_ntfy_when_not_configured(self, tmp_path):
        """No ntfy call at all when config.notifications.ntfy is None."""
        config = _make_config(tmp_path)  # no ntfy
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
        ):
            result = run_task(config, task, notify=True)

        assert result.success is True
        mock_ntfy.assert_not_called()

    def test_no_ntfy_when_notify_false(self, tmp_path):
        """No ntfy call when notify=False even if ntfy is configured."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
        ):
            result = run_task(config, task, notify=False)

        assert result.success is True
        assert not mock_ntfy.called

    def test_ntfy_publish_failure_is_silent(self, tmp_path):
        """If _ntfy_publish raises, the coordinator run still succeeds."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        with (
            patch(
                "theforge.coordinator.engine.run_agent",
                side_effect=_preflight_then(_make_agent_result(output="Done.")),
            ),
            patch(
                "theforge.coordinator.engine.run_agent_pool",
                return_value=_make_pool_result([APPROVE_REVIEW], ["review"]),
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch(
                "theforge.coordinator.engine._ntfy_publish",
                side_effect=OSError("network unreachable"),
            ),
        ):
            result = run_task(config, task, notify=True)

        assert result.success is True
        assert result.phase == Phase.DONE

    def test_already_done_publishes_ntfy(self, tmp_path):
        """ALREADY_DONE preflight verdict publishes a DONE-style ntfy notification."""
        config = _make_ntfy_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        preflight_already_done = _make_agent_result(
            success=True, output=PREFLIGHT_ALREADY_DONE, cost_usd=0.05
        )

        with (
            patch("theforge.coordinator.engine.run_agent", return_value=preflight_already_done),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
            patch("theforge.coordinator.engine.has_review_approve", return_value=True),
        ):
            result = run_task(config, task, notify=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        done_calls = [c for c in mock_ntfy.call_args_list if "done" in c.args[1]]
        assert len(done_calls) == 1
        title = done_calls[0].args[1]
        body = done_calls[0].args[2]
        assert "✓" in title
        assert "done" in title
        assert "APPROVE" in body
        assert "$" in body
        assert "Branch:" in body
