from __future__ import annotations

import dataclasses
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


class TestTransientRetryAttemptTelemetry:
    """A recovered transient failure must leave BOTH invocations in the record.

    #1388: a reviewer that times out / has a transport failure and then succeeds
    on retry is two distinct invocations. The failed one must not evaporate from
    reviewer_attempts just because a later retry replaced its result.
    """

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_recovered_transient_failure_records_both_invocations(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        r = _make_review_profile("r")
        config = _make_pool_config(tmp_path, [r], r)
        config = dataclasses.replace(
            config,
            retry=dataclasses.replace(
                config.retry,
                demotion_threshold=0,
                max_review_transport_retries=2,
                review_transport_retry_backoff_seconds=0.0,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # Initial pool invocation times out (transient); the retry then succeeds
        # with a parseable APPROVE.
        mock_pool.return_value = [
            _make_agent_result(success=False, output="", profile_name="r", failure_code="timeout")
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="r"
        )

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
        attempts = [a for a in state.reviewer_attempts if a["name"] == "r"]
        assert len(attempts) == 2
        assert sorted(a["completed_parseable_verdict"] for a in attempts) == [False, True]
        failed_attempt = next(a for a in attempts if not a["completed_parseable_verdict"])
        assert failed_attempt["outcome"] == "timeout"
        assert mock_run_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_exhausted_transient_retries_record_every_invocation(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        r = _make_review_profile("r")
        config = _make_pool_config(tmp_path, [r], r)
        config = dataclasses.replace(
            config,
            retry=dataclasses.replace(
                config.retry,
                demotion_threshold=0,
                max_review_transport_retries=2,
                review_transport_retry_backoff_seconds=0.0,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # Every invocation (initial + 2 retries) is a transient failure.
        mock_pool.return_value = [
            _make_agent_result(success=False, output="", profile_name="r", failure_code="timeout")
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=False, output="", profile_name="r", failure_code="timeout"
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
        )

        # 1 initial + 2 retries = 3 invocations, all recorded as failed.
        attempts = [a for a in state.reviewer_attempts if a["name"] == "r"]
        assert len(attempts) == 3
        assert all(not a["completed_parseable_verdict"] for a in attempts)


class TestParseRetryAttemptTelemetry:
    """A recovered parse failure must leave BOTH invocations in the record.

    #1388: a reviewer whose initial output is unparseable and whose parse retry
    then produces a valid verdict is two distinct invocations. The failed initial
    parse must not collapse into a single completed attempt just because a later
    retry parsed.
    """

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_recovered_parse_failure_records_both_invocations(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        r = _make_review_profile("r")
        config = _make_pool_config(tmp_path, [r], r)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, demotion_threshold=0)
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # Initial transport succeeds but output is unparseable; the parse retry
        # returns a valid APPROVE verdict.
        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r")
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="r"
        )

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
            max_review_parse_retries=2,
        )

        assert merged is not None
        attempts = [a for a in state.reviewer_attempts if a["name"] == "r"]
        assert len(attempts) == 2
        assert sorted(a["completed_parseable_verdict"] for a in attempts) == [False, True]
        failed_attempt = next(a for a in attempts if not a["completed_parseable_verdict"])
        assert failed_attempt["outcome"] == "parse_failure"
        assert mock_run_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_exhausted_parse_retries_record_every_invocation(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        r = _make_review_profile("r")
        config = _make_pool_config(tmp_path, [r], r)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, demotion_threshold=0)
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # Every invocation (initial + 2 parse retries) stays unparseable.
        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="r")
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=True, output=PARSE_ERROR_OUTPUT, profile_name="r"
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
            max_review_parse_retries=2,
        )

        # 1 initial + 2 parse retries = 3 invocations, all failed (parse_failure).
        attempts = [a for a in state.reviewer_attempts if a["name"] == "r"]
        assert len(attempts) == 3
        assert all(not a["completed_parseable_verdict"] for a in attempts)
        assert all(a["outcome"] == "parse_failure" for a in attempts)


class TestBudgetOverrunAttemptTelemetry:
    """A share-overrun reviewer's completion must reflect its ACTUAL output.

    #1388: completion cannot be proxied from transport success. A reviewer with
    unparseable output is NOT a completed verdict; one with a valid verdict IS.
    Parseability is established by parsing, never assumed from transport success.

    #2169: running over the derived share is telemetry, not an exclusion — the
    reviewer's verdict is retained and still counts toward the cycle.
    """

    def _config(self, tmp_path, over, keep):
        config = _make_pool_config(tmp_path, [over, keep], keep)
        return dataclasses.replace(
            config,
            retry=dataclasses.replace(
                config.retry, demotion_threshold=0, review_quorum_threshold=1
            ),
        )

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_budget_overrun_unparseable_records_not_completed(
        self, mock_pool, _mock_log, tmp_path
    ):
        over = _make_review_profile("over")
        keep = _make_review_profile("keep")
        config = self._config(tmp_path, over, keep)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # `over` returns UNPARSEABLE output and is over its share (cost 5.0 > 1.0).
        # The overrun is recorded; the unparseable output is what decides
        # completion, so it must NOT record as completed.
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=PARSE_ERROR_OUTPUT, profile_name="over", cost_usd=5.0
            ),
            _make_agent_result(
                success=True, output=APPROVE_REVIEW, profile_name="keep", cost_usd=0.1
            ),
        ]

        _successful, _failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=True,
        )

        assert merged is not None
        by_name = {a["name"]: a for a in state.reviewer_attempts}
        assert by_name["over"]["completed_parseable_verdict"] is False
        assert by_name["over"]["outcome"] == "budget_overrun"
        assert by_name["keep"]["completed_parseable_verdict"] is True

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_budget_overrun_parseable_records_completed(self, mock_pool, _mock_log, tmp_path):
        over = _make_review_profile("over")
        keep = _make_review_profile("keep")
        config = self._config(tmp_path, over, keep)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # `over` returns a VALID verdict and is over its share: it DID complete a
        # parseable verdict, and since #2169 the verdict is retained as well.
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=APPROVE_REVIEW, profile_name="over", cost_usd=5.0
            ),
            _make_agent_result(
                success=True, output=APPROVE_REVIEW, profile_name="keep", cost_usd=0.1
            ),
        ]

        meta = _meta()
        successful, _failed, _merged, _individual, named = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=True,
        )

        by_name = {a["name"]: a for a in state.reviewer_attempts}
        assert by_name["over"]["completed_parseable_verdict"] is True
        assert by_name["over"]["outcome"] == "budget_overrun"
        # #2169: the overrun is telemetry only — the reviewer that already ran
        # and already produced a verdict is still counted, not withdrawn.
        assert "over" in [r.profile_name for r in successful]
        assert "over" in meta.successful
        assert "over" in [name for name, _result in named]
        assert state.reviewer_budget_overruns == [
            {
                "reviewer": "over",
                "cycle": 1,
                "measured_usd": 5.0,
                "share_usd": 1.0,
                "overrun_usd": 4.0,
            }
        ]


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


class TestTransientRetryClassifier:
    def test_failure_code_rate_limit_is_transient(self, tmp_path):
        from theforge.coordinator.review_pool import _is_transient_review_failure

        config = _make_pool_config(
            tmp_path, [_make_review_profile("r1")], _make_review_profile("r1")
        )
        result = _make_agent_result(success=False, output="boom", profile_name="r1")
        result = result.__class__(**{**result.__dict__, "failure_code": "rate_limit"})
        assert _is_transient_review_failure(result, config) is True

    def test_failure_code_provider_internal_error_is_transient(self, tmp_path):
        from theforge.coordinator.review_pool import _is_transient_review_failure

        config = _make_pool_config(
            tmp_path, [_make_review_profile("r1")], _make_review_profile("r1")
        )
        result = _make_agent_result(success=False, output="boom", profile_name="r1")
        result = result.__class__(**{**result.__dict__, "failure_code": "provider_internal_error"})
        assert _is_transient_review_failure(result, config) is True

    def test_failure_code_connection_reset_is_transient(self, tmp_path):
        from theforge.coordinator.review_pool import _is_transient_review_failure

        config = _make_pool_config(
            tmp_path, [_make_review_profile("r1")], _make_review_profile("r1")
        )
        result = _make_agent_result(success=False, output="boom", profile_name="r1")
        result = result.__class__(**{**result.__dict__, "failure_code": "connection_reset"})
        assert _is_transient_review_failure(result, config) is True

    def test_exit1_empty_output_is_transient(self, tmp_path):
        from theforge.coordinator.review_pool import _is_transient_review_failure

        config = _make_pool_config(
            tmp_path, [_make_review_profile("r1")], _make_review_profile("r1")
        )
        result = _make_agent_result(success=False, output="", profile_name="r1")
        assert _is_transient_review_failure(result, config) is True

    def test_successful_result_not_transient(self, tmp_path):
        from theforge.coordinator.review_pool import _is_transient_review_failure

        config = _make_pool_config(
            tmp_path, [_make_review_profile("r1")], _make_review_profile("r1")
        )
        result = _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1")
        assert _is_transient_review_failure(result, config) is False

    def test_hard_failure_with_real_output_not_transient(self, tmp_path):
        from theforge.coordinator.review_pool import _is_transient_review_failure

        config = _make_pool_config(
            tmp_path, [_make_review_profile("r1")], _make_review_profile("r1")
        )
        result = _make_agent_result(
            success=False, output="ValueError: bad spec format", profile_name="r1"
        )
        assert _is_transient_review_failure(result, config) is False


class TestTransientRetry:
    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_transient_retry_succeeds_within_budget(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = _make_pool_config(tmp_path, [r1, r2], r1)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=2,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=False, output="", profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="r1"
        )

        meta = _meta()
        successful, failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is not None
        assert sorted(r.profile_name for r in successful) == ["r1", "r2"]
        assert failed == []
        assert meta.transient_retries.get("r1") == 1
        assert meta.transient_outcomes["r1"] == "transient_retried_then_succeeded"
        assert meta.transient_outcomes["r2"] == "succeeded"
        assert meta.quorum_met is True
        assert meta.quorum_threshold == 2
        assert mock_run_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_transient_retry_updates_reviewer_progress_state(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        """Production retry path: a transient-failed reviewer surfaces the retry
        glyph via the aggregator, and a resolved retry clears it and marks done
        (issue #1086, review cycle 1 P1)."""
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = _make_pool_config(tmp_path, [r1, r2], r1)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=2,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # r1 comes back transient-failed from the pool; the retry then succeeds.
        mock_pool.return_value = [
            _make_agent_result(success=False, output="", profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="r1"
        )

        captured: dict = {}

        def _state_update(updates: dict) -> None:
            detail = updates.get("detail")
            if isinstance(detail, dict) and "reviewer_progress" in detail:
                captured["detail"] = detail

        meta = _meta()
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
            state_update_fn=_state_update,
        )

        # The final emitted detail reflects the resolved retry: r1 is done and
        # carries no lingering retry marker (set_retry armed it, the success
        # cleared it).
        rp = captured["detail"]["reviewer_progress"]["r1"]
        assert rp["done"] is True
        assert rp["retry"] is None

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_transient_retry_exhausts_budget_records_failed_outcome(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        r3 = _make_review_profile("r3")
        r4 = _make_review_profile("r4")
        config = _make_pool_config(tmp_path, [r1, r2, r3, r4], r1)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=2,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=False, output="", profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r3"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r4"),
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=False, output="", profile_name="r1"
        )

        meta = _meta()
        successful, failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        # Quorum met (3 of 4 succeeded ≥ threshold 2) — proceed without escalation
        assert merged is not None
        assert state.phase != Phase.ESCALATE
        assert sorted(r.profile_name for r in successful) == ["r2", "r3", "r4"]
        assert [r.profile_name for r in failed] == ["r1"]
        assert meta.transient_retries["r1"] == 2
        assert meta.transient_outcomes["r1"] == "transient_retried_then_failed"
        assert meta.quorum_met is True
        assert mock_run_agent.call_count == 2

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_quorum_unmet_escalates(self, mock_pool, mock_run_agent, _mock_log, tmp_path):
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        r3 = _make_review_profile("r3")
        r4 = _make_review_profile("r4")
        config = _make_pool_config(tmp_path, [r1, r2, r3, r4], r1)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=0,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=False, output="ValueError: nope", profile_name="r1"),
            _make_agent_result(success=False, output="ValueError: nope", profile_name="r2"),
            _make_agent_result(success=False, output="ValueError: nope", profile_name="r3"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r4"),
        ]

        meta = _meta()
        _successful, failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert "Quorum unmet" in state.error
        assert len(failed) == 3
        assert meta.quorum_met is False
        assert meta.quorum_threshold == 2
        assert mock_run_agent.call_count == 0

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_quorum_unmet_retains_survivor_verdicts(
        self, mock_pool, _mock_run_agent, _mock_log, tmp_path
    ):
        """A collapse still escalates, but the surviving verdict must remain visible.

        #2300: the survivor's APPROVE used to vanish with the merged result, so a
        later full-knowledge operator decision had nothing to act on. merged stays
        None (quorum still governs automatic progression); the individual/named
        slots carry the verdict the reviewer actually produced.
        """
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = _make_pool_config(tmp_path, [r1, r2], r1)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=0,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=False, output="quota exceeded", profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        meta = _meta()
        _successful, _failed, merged, individual, named = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert "Quorum unmet" in state.error
        assert [n for n, _ in named] == ["r2"]
        assert named[0][1].verdict == "APPROVE"
        assert [r.verdict for r in individual] == ["APPROVE"]

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_panel_size_one_collapses_threshold(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        """Single-reviewer panel: threshold collapses to 1; success still proceeds."""
        r1 = _make_review_profile("solo")
        config = _make_pool_config(tmp_path, [r1], r1)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=0,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="solo")
        ]

        meta = _meta()
        _successful, _failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is not None
        assert meta.quorum_threshold == 1
        assert meta.quorum_met is True

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_panel_size_one_transient_fail_escalates(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        """Panel of 1 with no successful reviewers still escalates (collapsed threshold=1)."""
        r1 = _make_review_profile("solo")
        config = _make_pool_config(tmp_path, [r1], r1)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=1,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=False, output="", profile_name="solo")
        ]
        mock_run_agent.return_value = _make_agent_result(
            success=False, output="", profile_name="solo"
        )

        meta = _meta()
        _successful, _failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert meta.quorum_threshold == 1
        assert meta.quorum_met is False
        assert meta.transient_outcomes["solo"] == "transient_retried_then_failed"


class _CapturingLogger:
    """Minimal stand-in for the audit logger that records _safe_emit calls."""

    def __init__(self) -> None:
        self.emits: list[tuple[str, dict]] = []

    def _safe_emit(self, event: str, **fields: object) -> None:
        self.emits.append((event, dict(fields)))


_NO_SUBMIT_OUTPUT = "Agent finished without calling submit tool and produced no output"


def _no_submit_result(profile_name: str):
    """A reviewer that finished its turn without emitting a submit call."""
    return _make_agent_result(
        success=False,
        output=_NO_SUBMIT_OUTPUT,
        profile_name=profile_name,
        failure_code="no_submit_completion",
    )


class TestDegradedQuorum:
    """A no-submit reviewer failure must not kill a story on quorum: with at
    least one surviving verdict, the pool degrades to the survivors with an
    explicit audit warning instead of escalating (issue #1598)."""

    def _degrade_config(self, tmp_path, profiles, primary, *, enabled: bool = True):
        config = _make_pool_config(tmp_path, profiles, primary)
        return config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    max_review_transport_retries=0,
                    review_quorum_threshold=2,
                    review_transport_retry_backoff_seconds=0.0,
                    review_degrade_on_infra_failure=enabled,
                ),
            }
        )

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_no_submit_degrades_to_surviving_verdict(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = self._degrade_config(tmp_path, [r1, r2], r1)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _no_submit_result("r2"),
        ]

        logger = _CapturingLogger()
        meta = _meta()
        successful, failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
            logger=logger,
        )

        # Story lands a verdict instead of failing outright.
        assert merged is not None
        assert merged.verdict == "APPROVE"
        assert state.phase != Phase.ESCALATE
        assert [r.profile_name for r in successful] == ["r1"]
        assert [r.profile_name for r in failed] == ["r2"]
        # Recorded as degraded (threshold genuinely not met) with a warning.
        assert meta.quorum_met is False
        assert meta.degraded_quorum is True
        assert meta.degraded_quorum_warning
        assert "Degraded quorum" in meta.degraded_quorum_warning
        # Audit trail carries the degrade decision.
        assert any(event == "review_degraded_quorum" for event, _ in logger.emits)
        _, fields = next(e for e in logger.emits if e[0] == "review_degraded_quorum")
        assert fields["successful"] == ["r1"]
        assert fields["failed"] == ["r2"]

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_all_no_submit_no_survivor_escalates(
        self, mock_pool, mock_run_agent, _mock_log, tmp_path
    ):
        """Degrade needs a surviving verdict — zero survivors still escalates."""
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = self._degrade_config(tmp_path, [r1, r2], r1)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _no_submit_result("r1"),
            _no_submit_result("r2"),
        ]

        meta = _meta()
        _successful, _failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert "Quorum unmet" in state.error
        assert meta.degraded_quorum is False

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_hard_failure_does_not_degrade(self, mock_pool, mock_run_agent, _mock_log, tmp_path):
        """A genuine hard crash (not a non-verdict completion) still escalates —
        the degrade path is scoped to infrastructure/no-submit failures."""
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = self._degrade_config(tmp_path, [r1, r2], r1)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=False, output="ValueError: nope", profile_name="r2"),
        ]

        meta = _meta()
        _successful, _failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert meta.degraded_quorum is False

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_degrade_disabled_escalates(self, mock_pool, mock_run_agent, _mock_log, tmp_path):
        """review_degrade_on_infra_failure=False preserves fail-closed behavior."""
        r1 = _make_review_profile("r1")
        r2 = _make_review_profile("r2")
        config = self._degrade_config(tmp_path, [r1, r2], r1, enabled=False)
        task = _make_task(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _no_submit_result("r2"),
        ]

        meta = _meta()
        _successful, _failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=False,
        )

        assert merged is None
        assert state.phase == Phase.ESCALATE
        assert meta.degraded_quorum is False
