"""Global test fixtures and safety patches for the theforge test suite."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from theforge.config import ModelProfile


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
def _mock_pull_base_branch():
    """Stub pull_base_branch to return True for all tests by default.

    Tests that explicitly test pull behavior (e.g. workspace tests, pre-pull
    tests) override this with their own patch. The autouse stub prevents the
    new fail-closed behavior from breaking sprint tests that exercise parallel
    scheduling, DAG logic, etc. but don't care about git pull semantics.
    """
    with patch("theforge.sprint.runner.pull_base_branch", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _block_real_coordinator_runners():
    """Require tests to stub coordinator runner entry points explicitly.

    Accidental fallthrough into any coordinator runner path is both slow and
    misleading: it can spawn actual runner processes and watchdog threads from
    tests that only intended to exercise coordinator control flow.

    Tests that genuinely need one of these boundaries should patch that symbol
    explicitly in the test body or decorator stack.
    """

    def _unexpected_call(symbol: str) -> AssertionError:
        return AssertionError(f"Test hit real {symbol}; patch {symbol} explicitly.")

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
    ):
        yield
