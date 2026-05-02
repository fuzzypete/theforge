"""Tests for remote plan-review and terminal ntfy notification behaviour.

Classes:
- TestNtfyPollPlanReply — unit tests for _ntfy_poll_plan_reply()
- TestPlanReviewRemote — unit tests for _plan_review_remote()
- TestNtfyTerminalNotifications — ntfy publish at DONE / ESCALATE
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    PREFLIGHT_ALREADY_DONE,
    REQUEST_CHANGES_REVIEW,
    _make_agent_result,
    _make_config,
    _make_ntfy_config,
    _make_pool_result,
    _make_task,
    _shell_with_gate,
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
from theforge.coordinator.engine import run_task
from theforge.coordinator.ntfy_client import _ntfy_poll_plan_reply
from theforge.coordinator.remote_gates import _plan_review_remote
from theforge.coordinator.state import CoordinatorState, Phase


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
ac_verification:
  - criterion: "Implementation satisfies the spec"
    status: VERIFIED
    evidence: "diff hunks + tests (fixture default)"
```
"""
        with (
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                return_value=_make_agent_result(output="Done."),
            ),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
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
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                side_effect=[
                    _make_agent_result(output="Done."),
                    _make_agent_result(output="Fixed."),
                ],
            ),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
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
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                side_effect=[
                    _make_agent_result(output="Done."),
                    _make_agent_result(output="Fixed."),
                ],
            ),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
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
            patch(
                "theforge.coordinator.ntfy_client._ntfy_publish",
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
            patch(
                "theforge.coordinator.preflight_flow.run_agent",
                return_value=preflight_already_done,
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=_shell_with_gate(workspace)),
            patch("theforge.coordinator.notify._ntfy_publish") as mock_ntfy,
            patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=True),
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
