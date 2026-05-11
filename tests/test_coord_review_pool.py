from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    PARSE_ERROR_OUTPUT,
    _make_agent_result,
    _make_pool_config,
    _make_review_profile,
    _make_task,
)

from theforge.config import RetryPolicy
from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.state import CoordinatorState, Phase, ReviewCycleMetadata


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)


class TestReviewerDemotion:
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_reviewer_demoted_on_next_cycle_after_threshold(
        self, mock_pool, _mock_log_agent_result, tmp_path
    ):
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = _make_pool_config(tmp_path, [r1, r2], r1)
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(demotion_threshold=2)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.side_effect = [
            [
                _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1"),
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
            ],
            [
                _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1"),
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
            ],
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2")],
        ]

        with patch("theforge.coordinator.review_pool._log") as mock_log:
            for _ in range(2):
                successful, failed, merged, _, _ = _run_review_pool(
                    state,
                    config,
                    task,
                    "story",
                    workspace,
                    "branch",
                    _meta(),
                    notify=False,
                    enforce_budgets=False,
                )
                assert merged is not None
                assert [r.profile_name for r in successful] == ["r1", "r2"]
                assert failed == []

            successful, failed, merged, _, _ = _run_review_pool(
                state,
                config,
                task,
                "story",
                workspace,
                "branch",
                _meta(),
                notify=False,
                enforce_budgets=False,
            )

        assert merged is not None
        assert [r.profile_name for r in successful] == ["r2"]
        assert failed == []
        assert state.reviewer_parse_failure_counts == {"r1": 2}
        assert mock_pool.call_args_list[2].kwargs["profiles"] == [r2]
        assert any(
            "⚠ r1 demoted after 2 parse failures this run" in str(c)
            for c in mock_log.call_args_list
        )

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_demotion_threshold_zero_disables_demotion(
        self, mock_pool, _mock_log_agent_result, tmp_path
    ):
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = _make_pool_config(tmp_path, [r1, r2], r1)
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(demotion_threshold=0)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        for _ in range(3):
            successful, _, merged, _, _ = _run_review_pool(
                state,
                config,
                task,
                "story",
                workspace,
                "branch",
                _meta(),
                notify=False,
                enforce_budgets=False,
            )
            assert merged is not None
            assert [r.profile_name for r in successful] == ["r1", "r2"]

        assert state.reviewer_parse_failure_counts == {}
        assert all(call.kwargs["profiles"] == [r1, r2] for call in mock_pool.call_args_list)

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_all_reviewers_demoted_escalates(self, mock_pool, _mock_log_agent_result, tmp_path):
        reviewer = _make_review_profile("solo")
        config = _make_pool_config(tmp_path, [reviewer], reviewer)
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(demotion_threshold=1)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="solo")
        ]

        successful, failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
        )
        assert merged is not None
        assert [r.profile_name for r in successful] == ["solo"]
        assert failed == []

        successful, failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
        )

        assert successful == []
        assert failed == []
        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert state.error == "All reviewers demoted due to parse failures."
        assert mock_pool.call_count == 1

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_threshold_one_demotes_after_single_failure(
        self, mock_pool, _mock_log_agent_result, tmp_path
    ):
        reviewer = _make_review_profile("solo")
        config = _make_pool_config(tmp_path, [reviewer], reviewer)
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(demotion_threshold=1)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="solo")
        ]

        with patch("theforge.coordinator.review_pool._log") as mock_log:
            _run_review_pool(
                state,
                config,
                task,
                "story",
                workspace,
                "branch",
                _meta(),
                notify=False,
                enforce_budgets=False,
            )

        assert state.reviewer_parse_failure_counts == {"solo": 1}
        assert any(
            "⚠ solo demoted after 1 parse failures this run" in str(c)
            for c in mock_log.call_args_list
        )

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_failures_below_threshold_across_cycles_not_demoted(
        self, mock_pool, _mock_log_agent_result, tmp_path
    ):
        """Reviewer fails once per cycle but never reaches threshold in a cycle — not demoted."""
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = _make_pool_config(tmp_path, [r1, r2], r1)
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(demotion_threshold=3)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        # Cycle 1: r1 fails once (count=1, < threshold=3)
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
        )
        assert state.reviewer_parse_failure_counts == {"r1": 1}

        # Simulate cycle boundary: _run_review_phase resets the per-cycle counter
        state.reviewer_parse_failure_counts = {}

        # Cycle 2: r1 fails again (starts fresh from 0, count=1 again, still < threshold=3)
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
        )
        assert state.reviewer_parse_failure_counts == {"r1": 1}

        # r1 never hit the threshold in any single cycle → not demoted
        assert "r1" not in state.reviewer_demoted
        # Both cycles used the full pool (r1 was not filtered out)
        assert all(call.kwargs["profiles"] == [r1, r2] for call in mock_pool.call_args_list)

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_demotion_persists_across_cycle_boundary(
        self, mock_pool, _mock_log_agent_result, tmp_path
    ):
        """Reviewer that hits threshold within one cycle stays demoted after counter reset."""
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = _make_pool_config(tmp_path, [r1, r2], r1)
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(demotion_threshold=1)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.side_effect = [
            # Cycle 1: r1 fails (threshold=1 → immediately demoted)
            [
                _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1"),
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
            ],
            # Cycle 2: only r2 should be in pool
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2")],
        ]

        # Cycle 1: r1 hits threshold → added to reviewer_demoted
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
        )
        assert "r1" in state.reviewer_demoted

        # Simulate cycle boundary: _run_review_phase resets the per-cycle counter
        state.reviewer_parse_failure_counts = {}

        # Cycle 2: counter is clear, but demotion set preserves r1's demotion
        successful, _, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
        )
        assert merged is not None
        assert [r.profile_name for r in successful] == ["r2"]
        assert mock_pool.call_args_list[1].kwargs["profiles"] == [r2]


class TestRetryTraceFiles:
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_failed_retry_writes_trace_file(
        self, mock_pool, mock_run_agent, _mock_log_agent_result, tmp_path
    ):
        """Retry attempts that fail to parse must still be written to disk."""
        reviewer = _make_review_profile("r1")
        config = _make_pool_config(tmp_path, [reviewer], reviewer)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # Initial pool call returns unparseable output
        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1")
        ]
        # Retry via run_agent also returns unparseable output
        mock_run_agent.return_value = _make_agent_result(
            success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1"
        )

        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
            max_review_parse_retries=1,
        )

        # _cycle_num = state.review_cycle + 1 = 1, pool_attempt = 0
        trace_file = workspace / ".forge" / "traces" / "1-0-review-r1-retry1.txt"
        assert trace_file.exists(), (
            "Failed retry must write trace file regardless of parse outcome"
        )
        assert trace_file.read_text(encoding="utf-8") == PARSE_ERROR_OUTPUT

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_successful_retry_also_writes_trace_file(
        self, mock_pool, mock_run_agent, _mock_log_agent_result, tmp_path
    ):
        """Successful retries must continue to write trace files (regression guard)."""
        reviewer = _make_review_profile("r1")
        config = _make_pool_config(tmp_path, [reviewer], reviewer)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r1")
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="r1"
        )

        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
            max_review_parse_retries=1,
        )

        trace_file = workspace / ".forge" / "traces" / "1-0-review-r1-retry1.txt"
        assert trace_file.exists(), "Successful retry must also write trace file"
        assert trace_file.read_text(encoding="utf-8") == APPROVE_REVIEW
