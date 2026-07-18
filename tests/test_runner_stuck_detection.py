"""Progress-aware stuck-agent detection in the API agent loop."""

from __future__ import annotations

from pathlib import Path

from theforge.agent_types import ModelUsage
from theforge.config import ModelProfile, StuckDetectionConfig
from theforge.runners.api import AgentLoopManager
from theforge.runners.schema_utils import (
    SUBMIT_REVIEW,
    LoopTurn,
    ToolCallRequest,
)
from theforge.runners.tool_runtime import TOOL_REGISTRY


def _dev_profile(
    *,
    stuck: StuckDetectionConfig | None,
    allowed_tools: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    timeout_seconds: int = 300,
) -> ModelProfile:
    return ModelProfile(
        name="test-dev",
        provider="openai",
        cli=None,
        model="gpt-4o",
        budget_usd=1.0,
        timeout_seconds=timeout_seconds,
        allowed_tools=allowed_tools,
        phase="dev",
        stuck_detection=stuck,
    )


def _usage() -> ModelUsage:
    return ModelUsage(
        model="gpt-4o",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=None,
    )


def _make_manager(profile: ModelProfile, working_dir: Path, adapter) -> AgentLoopManager:
    return AgentLoopManager(
        profile=profile,
        provider="openai",
        working_dir=working_dir,
        tools=list(TOOL_REGISTRY.values()),
        provider_adapter=adapter,
        max_iterations=50,
    )


def _glob_call(idx: int, pattern: str = "*.py") -> ToolCallRequest:
    return ToolCallRequest(id=f"c{idx}", name="glob", arguments={"pattern": pattern})


def _glob_turn(idx: int, pattern: str = "*.py") -> LoopTurn:
    return LoopTurn(
        tool_calls=[_glob_call(idx, pattern=pattern)],
        text_output=None,
        structured_data=None,
        usage=_usage(),
    )


class TestStuckDetectionTriggers:
    """The runner injects a nudge once the repeat-call threshold is crossed."""

    def test_repeated_identical_calls_trigger_nudge(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=3,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        messages_seen: list[list[dict]] = []
        call_count = [0]

        def adapter(messages, tools):
            messages_seen.append(list(messages))
            call_count[0] += 1
            if call_count[0] <= 5:
                return _glob_turn(call_count[0])
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="submit",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_usage(),
            )

        manager = _make_manager(profile, tmp_path, adapter)
        manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        # A stuck nudge should have been injected.
        all_msgs = [m for msgs in messages_seen for m in msgs]
        nudges = [
            m
            for m in all_msgs
            if m.get("role") == "user" and "Progress check" in m.get("content", "")
        ]
        assert len(nudges) >= 1
        assert "stuck" in nudges[0]["content"].lower()


class TestStuckDetectionNudgeDelivery:
    """Detection only fires once per run, regardless of how long the pattern persists."""

    def test_nudge_sent_only_once(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=99,  # never terminate, just count nudges
        )
        profile = _dev_profile(stuck=cfg)
        messages_seen: list[list[dict]] = []
        call_count = [0]

        def adapter(messages, tools):
            messages_seen.append(list(messages))
            call_count[0] += 1
            if call_count[0] <= 8:
                return _glob_turn(call_count[0])
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="submit",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "done"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_usage(),
            )

        manager = _make_manager(profile, tmp_path, adapter)
        manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        # Use the final message list — the nudge user-message persists in history.
        last_msgs = messages_seen[-1]
        progress_nudges = [
            m
            for m in last_msgs
            if m.get("role") == "user" and "Progress check" in m.get("content", "")
        ]
        assert len(progress_nudges) == 1


class TestStuckDetectionTermination:
    """Termination follows when the nudge does not break the pattern."""

    def test_termination_after_nudge_persistence(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=3,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=2,
        )
        profile = _dev_profile(stuck=cfg)
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            return _glob_turn(call_count[0])

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )

        assert not result.success
        assert "stuck pattern" in result.output.lower()
        assert "after nudge" in result.output.lower()
        # nudge at iter 3, then 2 more identical iters → terminate at iter 5.
        assert call_count[0] == 5

    def test_termination_logs_pattern_and_counts(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=2,
        )
        profile = _dev_profile(stuck=cfg)
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            return _glob_turn(call_count[0])

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )

        # Reason mentions iteration count + tool count + the pattern.
        out = result.output
        assert "repeated identical tool calls" in out
        assert "iteration" in out


class TestStuckDetectionNoFalsePositiveOnVariedIterations:
    """Different tool calls per iteration are real progress, not a stuck pattern."""

    def test_varied_signatures_do_not_trigger(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=3,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=2,
        )
        profile = _dev_profile(stuck=cfg)
        messages_seen: list[list[dict]] = []
        call_count = [0]
        patterns = ["*.py", "*.md", "*.txt", "src/*", "tests/*", "*.json", "*.yaml"]

        def adapter(messages, tools):
            messages_seen.append(list(messages))
            call_count[0] += 1
            if call_count[0] <= len(patterns):
                return _glob_turn(call_count[0], pattern=patterns[call_count[0] - 1])
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="submit",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_usage(),
            )

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success

        all_msgs = [m for msgs in messages_seen for m in msgs]
        nudges = [
            m
            for m in all_msgs
            if m.get("role") == "user" and "Progress check" in m.get("content", "")
        ]
        assert nudges == []


class TestStuckDetectionDisabled:
    """When stuck_detection is None or phase != 'dev', the runner is unaffected."""

    def test_disabled_when_phase_not_dev(self, tmp_path):
        cfg = StuckDetectionConfig(enabled=True, repeat_threshold=2, post_nudge_iterations=2)
        # phase=None disables detection regardless of cfg.enabled.
        profile = ModelProfile(
            name="reviewer",
            provider="openai",
            cli=None,
            model="gpt-4o",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Glob"),
            phase=None,
            stuck_detection=cfg,
        )
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] <= 5:
                return _glob_turn(call_count[0])
            return LoopTurn(
                tool_calls=[],
                text_output="done",
                structured_data=None,
                usage=_usage(),
            )

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success

    def test_disabled_when_cfg_none(self, tmp_path):
        profile = _dev_profile(stuck=None)
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] <= 5:
                return _glob_turn(call_count[0])
            return LoopTurn(
                tool_calls=[],
                text_output="done",
                structured_data=None,
                usage=_usage(),
            )

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success


class TestNoProgressIsTelemetryOnly:
    """no_progress_iterations is recorded as telemetry but never nudges or terminates."""

    def test_no_modifications_does_not_terminate_or_nudge(self, tmp_path):
        # Aggressive thresholds: even with no_progress_iterations=2, the run must
        # complete normally because exploration is no longer a kill signal.
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=99,
            no_progress_iterations=2,
            error_threshold=99,
            post_nudge_iterations=2,
        )
        profile = _dev_profile(stuck=cfg)
        messages_seen: list[list[dict]] = []
        call_count = [0]
        patterns = ["*.py", "*.md", "*.txt", "src/*", "tests/*", "docs/*", "README*"]

        def adapter(messages, tools):
            messages_seen.append(list(messages))
            call_count[0] += 1
            if call_count[0] <= len(patterns):
                return _glob_turn(call_count[0], pattern=patterns[call_count[0] - 1])
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="submit",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_usage(),
            )

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success, result.output
        all_msgs = [m for msgs in messages_seen for m in msgs]
        progress_nudges = [
            m
            for m in all_msgs
            if m.get("role") == "user" and "Progress check" in m.get("content", "")
        ]
        assert progress_nudges == [], (
            "no-modification streak must not produce a stuck-detection nudge"
        )

    def test_tracker_records_no_progress_streak_as_telemetry(self):
        from theforge.runners.stuck_detection import (
            IterationObservation,
            StuckTracker,
        )

        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=99,
            no_progress_iterations=3,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        tracker = StuckTracker(profile)
        for i in range(5):
            obs = IterationObservation(
                signatures=frozenset({f"glob|{i}"}),
                successful_modify=False,
                error_content=None,
            )
            assert tracker.observe(obs) == (None, None, None)
        assert tracker.iters_without_modification == 5
        ev = tracker.evidence()
        assert ev["iters_without_modification"] == 5
        assert ev["iterations_observed"] == 5
        assert ev["active_kind"] is None  # no defensible pattern firing


class TestPostNudgeSamePatternRequired:
    """Post-nudge termination requires the SAME pattern kind to persist."""

    def test_pattern_change_after_nudge_resets_and_rearm(self, tmp_path):
        # repeat fires first; switch to varied calls (pattern breaks); the
        # tracker must re-arm rather than terminate. The second pattern is
        # error_loop (the other defensible signal still in scope), proving
        # that re-arm allows a fresh nudge for a different kind.
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=2,
            # Loose post-nudge window so the test isolates the re-arm behavior.
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        # iters 1,2: identical glob → repeat nudge fires at iter 2.
        # iters 3,4: distinct glob args, but each result errors with the same
        # message → error_loop must trigger a fresh nudge after the kind
        # change resets the post-nudge counter.
        scripts: list[tuple[str, str | None]] = [
            ("x", None),
            ("x", None),
            ("a", "Error: same boom"),
            ("b", "Error: same boom"),
            ("c", None),
            ("d", None),
        ]

        from theforge.runners.stuck_detection import (
            IterationObservation,
            StuckTracker,
        )

        tracker = StuckTracker(profile)
        nudge_kinds: list[str] = []
        for i, (pattern, err) in enumerate(scripts, start=1):
            obs = IterationObservation(
                signatures=frozenset({f"glob|{pattern}"}),
                successful_modify=False,
                error_content=err,
            )
            nudge, terminate, _ = tracker.observe(obs)
            assert terminate is None, f"terminate fired unexpectedly at iter {i}"
            if nudge is not None:
                if "repeated" in nudge:
                    nudge_kinds.append("repeat")
                elif "error loop" in nudge:
                    nudge_kinds.append("error_loop")
        assert "repeat" in nudge_kinds
        assert "error_loop" in nudge_kinds, (
            f"expected a fresh error_loop nudge after the repeat pattern broke; got {nudge_kinds}"
        )

    def test_no_terminate_when_post_nudge_pattern_breaks(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=2,
        )
        profile = _dev_profile(stuck=cfg)
        call_count = [0]
        # iter 1,2: identical → nudge at iter 2.
        # iter 3,4: different signatures → pattern broken, no terminate.
        # iter 5: submit.
        patterns = ["x", "x", "a", "b"]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] <= len(patterns):
                return _glob_turn(call_count[0], pattern=patterns[call_count[0] - 1])
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="submit",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_usage(),
            )

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success, result.output


class TestAuditObservability:
    """When the detector fires, the audit must include per-iteration evidence."""

    def test_termination_reason_includes_per_iteration_calls_and_loop_start(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=2,
        )
        profile = _dev_profile(stuck=cfg)
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            return _glob_turn(call_count[0], pattern="*.py")  # identical args every iter

        manager = _make_manager(profile, tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        out = result.output
        # New evidence block must appear in the audit reason.
        assert "signal: repeat" in out, out
        assert "loop began at iteration" in out, out
        assert "recent iteration tool calls:" in out, out
        # Per-iteration fingerprint shows the redacted/recorded call.
        assert "iter 1:" in out, out

    def test_evidence_redacts_sensitive_arguments(self):
        from theforge.runners.stuck_detection import (
            StuckTracker,
            build_observation,
        )

        class _Call:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments

        # Build one observation with a Write call that has a large `content` arg.
        obs = build_observation(
            calls=[_Call("Write", {"path": "x.py", "content": "secret-body" * 50})],
            results=[{"id": "c1", "name": "Write", "content": "ok"}],
        )
        # The audit fingerprint must mask the `content` value.
        assert any("redacted" in fp for fp in obs.call_fingerprints), obs.call_fingerprints
        assert all("secret-body" not in fp for fp in obs.call_fingerprints)

        # The internal signature (used for repeat detection) is still
        # discriminative — redaction is audit-only.
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        tracker = StuckTracker(profile)
        tracker.observe(obs)
        ev = tracker.evidence()
        assert ev["history"][0]["calls"]
        assert "redacted" in ev["history"][0]["calls"][0]

    def test_evidence_active_kind_matches_configured_repeat_threshold(self):
        from theforge.runners.stuck_detection import (
            PATTERN_REPEAT,
            StuckTracker,
            build_observation,
        )

        class _Call:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments

        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=5,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        tracker = StuckTracker(profile)

        # Two identical calls: below the configured threshold of 5, so
        # evidence() must not report an active pattern yet.
        for _ in range(2):
            obs = build_observation(
                calls=[_Call("Read", {"path": "x.py"})],
                results=[{"id": "c1", "name": "Read", "content": "ok"}],
            )
            tracker.observe(obs)
        ev = tracker.evidence()
        assert ev["repeat_count"] == 2
        assert ev["active_kind"] is None
        assert ev["loop_start_iteration"] is None

        # Reaching the configured threshold flips evidence() active.
        for _ in range(3):
            obs = build_observation(
                calls=[_Call("Read", {"path": "x.py"})],
                results=[{"id": "c1", "name": "Read", "content": "ok"}],
            )
            tracker.observe(obs)
        ev = tracker.evidence()
        assert ev["repeat_count"] == 5
        assert ev["active_kind"] == PATTERN_REPEAT

    def test_evidence_handles_missing_cfg(self):
        from theforge.runners.stuck_detection import StuckTracker, build_observation

        class _Call:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments

        profile = _dev_profile(stuck=None)
        tracker = StuckTracker(profile)
        for _ in range(5):
            obs = build_observation(
                calls=[_Call("Read", {"path": "x.py"})],
                results=[{"id": "c1", "name": "Read", "content": "ok"}],
            )
            tracker.observe(obs)
        ev = tracker.evidence()
        assert ev["active_kind"] is None
        assert ev["loop_start_iteration"] is None


class TestExplorationTelemetry:
    """Distinct file reads / searches across iterations are recorded as exploration."""

    def test_distinct_reads_advance_exploration_counter(self):
        from theforge.runners.stuck_detection import (
            IterationObservation,
            StuckTracker,
        )

        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=99,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        tracker = StuckTracker(profile)
        files = ["a.py", "b.py", "c.py", "d.py"]
        for i, f in enumerate(files):
            sig = f"Read|{f}"
            obs = IterationObservation(
                signatures=frozenset({sig}),
                successful_modify=False,
                error_content=None,
                exploration_sigs=frozenset({sig}),
            )
            tracker.observe(obs)
        assert tracker.distinct_exploration_count == len(files)
        assert tracker.exploration_progress_iters == len(files)

    def test_repeat_reads_do_not_advance_exploration_counter(self):
        from theforge.runners.stuck_detection import (
            IterationObservation,
            StuckTracker,
        )

        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=99,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        tracker = StuckTracker(profile)
        for _ in range(4):
            sig = "Read|same.py"
            obs = IterationObservation(
                signatures=frozenset({sig}),
                successful_modify=False,
                error_content=None,
                exploration_sigs=frozenset({sig}),
            )
            tracker.observe(obs)
        assert tracker.distinct_exploration_count == 1
        assert tracker.exploration_progress_iters == 1


class TestFailedModifyTelemetry:
    """A write/edit that returns an error must not count as modification progress.

    The no-progress arm is telemetry-only now, so this asserts the counter is
    incremented (not that termination/nudge fires).
    """

    def test_failed_edit_calls_increment_no_progress_counter(self, tmp_path):
        from theforge.runners.stuck_detection import (
            IterationObservation,
            StuckTracker,
        )

        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=99,
            no_progress_iterations=3,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        tracker = StuckTracker(profile)
        for i in range(5):
            obs = IterationObservation(
                signatures=frozenset({f"write_file|p{i}"}),
                successful_modify=False,  # write returned Error
                error_content=None,
            )
            nudge, terminate, _ = tracker.observe(obs)
            assert nudge is None
            assert terminate is None
        assert tracker.iters_without_modification == 5


class TestClaudeCliStuckDetection:
    """Stuck detection runs in the Claude CLI streaming loop, not just API mode."""

    @staticmethod
    def _assistant_event(call_id: str, name: str, args: dict) -> str:
        import json as _json

        return (
            _json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": name,
                                "input": args,
                            }
                        ]
                    },
                }
            )
            + "\n"
        )

    @staticmethod
    def _user_result_event(call_id: str, text: str, is_error: bool = False) -> str:
        import json as _json

        return (
            _json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "content": text,
                                "is_error": is_error,
                            }
                        ]
                    },
                }
            )
            + "\n"
        )

    @staticmethod
    def _result_event(text: str = "done") -> str:
        import json as _json

        return _json.dumps({"type": "result", "result": text, "session_id": "s1"}) + "\n"

    def _build_dev_profile(self, cfg: StuckDetectionConfig | None) -> ModelProfile:
        return ModelProfile(
            name="dev",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=120,
            allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
            phase="dev",
            stuck_detection=cfg,
        )

    def _build_stream(self, n_iterations: int) -> list[str]:
        """Build a Claude CLI stream of N identical Glob iterations."""
        lines: list[str] = []
        for i in range(n_iterations):
            cid = f"call-{i}"
            lines.append(self._assistant_event(cid, "Glob", {"pattern": "**/*.py"}))
            lines.append(self._user_result_event(cid, "[]"))
        lines.append(self._result_event())
        return lines

    def test_cli_terminates_on_stuck_pattern(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from theforge.runners.runner_claude import _run_claude

        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=2,
        )
        profile = self._build_dev_profile(cfg)

        # 10 identical Glob iterations is well past repeat(2) + post_nudge(2).
        stream = self._build_stream(10)

        mock_proc = MagicMock()
        mock_proc.stdout = iter(stream)
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = ""
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        # No such process → os.getpgid raises, so the runner's kill takes its
        # documented direct-child fallback (proc.kill) instead of killpg-ing a
        # bogus group id derived from a mock pid. The real group-kill path is
        # covered by the fake-CLI subprocess tests in test_process_group.py.
        mock_proc.pid = 2_000_000_000

        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = _run_claude(
                prompt="go",
                profile=profile,
                working_dir=tmp_path,
            )

        assert result.success is False
        assert result.failure_code == "stuck_pattern"
        assert "stuck pattern persisted" in result.output
        assert result.exit_code == -2
        # Subprocess must have been killed once a stuck termination fired.
        mock_proc.kill.assert_called()
        # Nudge must have been written to stdin as a stream-json user
        # message before termination — verify by inspecting the writes.
        import json as _json

        stdin_writes = [_call.args[0] for _call in mock_proc.stdin.write.call_args_list]
        # First write is the initial prompt; subsequent writes include the nudge.
        decoded = []
        for raw in stdin_writes:
            for piece in raw.splitlines():
                if not piece.strip():
                    continue
                try:
                    decoded.append(_json.loads(piece))
                except _json.JSONDecodeError:
                    pass
        user_contents = [
            d.get("message", {}).get("content", "") for d in decoded if d.get("type") == "user"
        ]
        assert user_contents, "expected at least the initial prompt written to stdin"
        assert user_contents[0] == "go"
        # At least one nudge user-message must have been delivered.
        nudge_msgs = [c for c in user_contents[1:] if "Progress check" in c]
        assert nudge_msgs, "expected stuck-detection nudge to be written to stdin"

    def test_cli_nudge_delivered_then_recovers(self, tmp_path):
        """Nudge is delivered via stdin; if pattern breaks, run completes normally."""
        from unittest.mock import MagicMock, patch

        from theforge.runners.runner_claude import _run_claude

        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=99,
            error_threshold=99,
            post_nudge_iterations=99,  # never terminate, isolate nudge delivery
        )
        profile = self._build_dev_profile(cfg)

        # 3 identical iterations → repeat nudge fires; then result event ends
        # the stream cleanly (no terminate because post_nudge=99).
        stream: list[str] = []
        for i in range(3):
            cid = f"call-{i}"
            stream.append(self._assistant_event(cid, "Glob", {"pattern": "*.py"}))
            stream.append(self._user_result_event(cid, "[]"))
        stream.append(self._result_event())

        mock_proc = MagicMock()
        mock_proc.stdout = iter(stream)
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = ""
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0

        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            _run_claude(prompt="go", profile=profile, working_dir=tmp_path)

        import json as _json

        stdin_writes = [c.args[0] for c in mock_proc.stdin.write.call_args_list]
        decoded = []
        for raw in stdin_writes:
            for piece in raw.splitlines():
                if not piece.strip():
                    continue
                try:
                    decoded.append(_json.loads(piece))
                except _json.JSONDecodeError:
                    pass
        user_msgs = [d["message"]["content"] for d in decoded if d.get("type") == "user"]
        # Initial prompt + at least one nudge.
        assert user_msgs[0] == "go"
        assert any("Progress check" in m for m in user_msgs[1:]), (
            f"expected nudge in stdin writes; got {user_msgs}"
        )
        # No kill since post_nudge=99 and the result event closed the stream.
        mock_proc.kill.assert_not_called()
        # stdin closed exactly once at end of stream.
        mock_proc.stdin.close.assert_called()

    def test_cli_does_not_terminate_when_phase_not_dev(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from theforge.runners.runner_claude import _run_claude

        # phase=None disables stuck detection regardless of cfg.enabled.
        cfg = StuckDetectionConfig(enabled=True, repeat_threshold=2, post_nudge_iterations=2)
        profile = ModelProfile(
            name="reviewer",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=120,
            allowed_tools=("Read", "Glob"),
            phase=None,
            stuck_detection=cfg,
        )

        stream = self._build_stream(10)
        mock_proc = MagicMock()
        mock_proc.stdout = iter(stream)
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = ""
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0

        with patch("theforge.runners.runner_claude.subprocess.Popen", return_value=mock_proc):
            result = _run_claude(
                prompt="go",
                profile=profile,
                working_dir=tmp_path,
            )
        # No stuck termination should have fired for non-dev phase.
        assert result.failure_code != "stuck_pattern"
        # And the stream completes through the result event.
        mock_proc.kill.assert_not_called()


class TestStuckDetectionConfigParsing:
    """Loader accepts and validates the forge.yaml stuck_detection block."""

    def test_parse_defaults(self):
        from theforge.config.load import _parse_stuck_detection

        cfg = _parse_stuck_detection({})
        assert cfg == StuckDetectionConfig()

    def test_parse_overrides(self):
        from theforge.config.load import _parse_stuck_detection

        cfg = _parse_stuck_detection(
            {
                "enabled": False,
                "no_progress_iterations": 7,
                "repeat_threshold": 5,
                "error_threshold": 6,
                "post_nudge_iterations": 4,
            }
        )
        assert cfg == StuckDetectionConfig(
            enabled=False,
            no_progress_iterations=7,
            repeat_threshold=5,
            error_threshold=6,
            post_nudge_iterations=4,
        )

    def test_parse_rejects_non_int_threshold(self):
        from theforge.config.load import _parse_stuck_detection

        try:
            _parse_stuck_detection({"repeat_threshold": "lots"})
        except ValueError as exc:
            assert "repeat_threshold" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_parse_rejects_zero_threshold(self):
        from theforge.config.load import _parse_stuck_detection

        try:
            _parse_stuck_detection({"no_progress_iterations": 0})
        except ValueError as exc:
            assert "no_progress_iterations" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_parse_multiplier_overrides(self):
        from theforge.config.load import _parse_stuck_detection

        cfg = _parse_stuck_detection(
            {
                "no_progress_multipliers": {"small": 1.0, "medium": 2.0, "large": 3.5},
                "post_nudge_multipliers": {"small": 1.0, "medium": 1.75, "large": 2.5},
            }
        )
        assert cfg.no_progress_multipliers == {"small": 1.0, "medium": 2.0, "large": 3.5}
        assert cfg.post_nudge_multipliers == {"small": 1.0, "medium": 1.75, "large": 2.5}

    def test_parse_multiplier_defaults_when_absent(self):
        from theforge.config.load import _parse_stuck_detection

        cfg = _parse_stuck_detection({"no_progress_iterations": 7})
        defaults = StuckDetectionConfig()
        assert cfg.no_progress_multipliers == defaults.no_progress_multipliers
        assert cfg.post_nudge_multipliers == defaults.post_nudge_multipliers

    def test_parse_rejects_non_mapping_multiplier(self):
        from theforge.config.load import _parse_stuck_detection

        try:
            _parse_stuck_detection({"no_progress_multipliers": [1.0, 2.0]})
        except ValueError as exc:
            assert "no_progress_multipliers" in str(exc) and "mapping" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_parse_rejects_non_string_multiplier_key(self):
        from theforge.config.load import _parse_stuck_detection

        try:
            _parse_stuck_detection({"post_nudge_multipliers": {3: 1.0}})
        except ValueError as exc:
            assert "post_nudge_multipliers" in str(exc) and "string" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_parse_rejects_non_positive_multiplier_value(self):
        from theforge.config.load import _parse_stuck_detection

        for bad in (0, -1.0, "fast", True):
            try:
                _parse_stuck_detection({"no_progress_multipliers": {"large": bad}})
            except ValueError as exc:
                assert "no_progress_multipliers" in str(exc) and "positive" in str(exc)
            else:
                raise AssertionError(f"expected ValueError for {bad!r}")

    def test_parse_rejects_non_finite_multiplier_value(self):
        from theforge.config.load import _parse_stuck_detection

        for bad in (float("nan"), float("inf"), float("-inf")):
            try:
                _parse_stuck_detection({"post_nudge_multipliers": {"large": bad}})
            except ValueError as exc:
                assert "post_nudge_multipliers" in str(exc) and "finite" in str(exc)
            else:
                raise AssertionError(f"expected ValueError for {bad!r}")
