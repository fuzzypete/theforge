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


class TestNoProgressDetection:
    """no_progress_iterations triggers when modify-capable agents stop modifying files."""

    def test_no_modifications_triggers_nudge(self, tmp_path):
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=99,
            no_progress_iterations=3,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        messages_seen: list[list[dict]] = []
        call_count = [0]
        # Vary the args so repeat_threshold doesn't fire instead.
        patterns = ["*.py", "*.md", "*.txt", "src/*", "tests/*"]

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
        manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        all_msgs = [m for msgs in messages_seen for m in msgs]
        nudges = [
            m
            for m in all_msgs
            if m.get("role") == "user" and "no file modifications" in m.get("content", "")
        ]
        assert len(nudges) >= 1


class TestPostNudgeSamePatternRequired:
    """Post-nudge termination requires the SAME pattern kind to persist."""

    def test_pattern_change_after_nudge_resets_and_rearm(self, tmp_path):
        # repeat fires first; switch to varied calls (pattern breaks); the
        # tracker must re-arm rather than terminate. Configure thresholds so
        # the second pattern (no-progress) would otherwise hit terminate
        # immediately if the kind check were absent.
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=2,
            no_progress_iterations=2,
            error_threshold=99,
            # Loose post-nudge window so the test isolates the re-arm behavior.
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        messages_seen: list[list[dict]] = []
        call_count = [0]
        # First two iterations identical → repeat nudge at iter 2.
        # Then varied iterations break repeat; tracker must re-arm and emit a
        # fresh no-progress nudge rather than terminating without one.
        patterns = ["x", "x", "a", "b", "c", "d", "e", "f"]

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
        # The run should not terminate on stuck pattern: repeat broke before
        # post-nudge persistence threshold was reached.
        assert result.success, result.output
        # Two distinct nudges expected: one for repeat, one for no-progress.
        last_msgs = messages_seen[-1]
        nudges = [
            m
            for m in last_msgs
            if m.get("role") == "user" and "Progress check" in m.get("content", "")
        ]
        # At least the repeat nudge must have been issued; the no-progress
        # arm needs >=2 iterations without modifications, satisfied by the
        # varied glob iterations. Both kinds should produce a nudge.
        kinds = {("repeated" in n["content"], "no file" in n["content"]) for n in nudges}
        assert any(repeat for repeat, _ in kinds)
        assert any(noprog for _, noprog in kinds), (
            "expected a fresh no-progress nudge after the repeat pattern broke"
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


class TestFailedModifyDoesNotResetNoProgress:
    """A write/edit that returns an error must not count as progress."""

    def test_failed_edit_calls_count_as_no_progress(self, tmp_path):
        # write_file with missing required arg → tool returns Error, so the
        # call should NOT reset the no-progress counter.
        cfg = StuckDetectionConfig(
            enabled=True,
            repeat_threshold=99,
            no_progress_iterations=3,
            error_threshold=99,
            post_nudge_iterations=99,
        )
        profile = _dev_profile(stuck=cfg)
        messages_seen: list[list[dict]] = []
        call_count = [0]
        # Each iteration calls write_file with a different (still-invalid)
        # path argument so signatures vary (no repeat) and the tool errors
        # (no successful modify). After 3 such iterations, no-progress
        # nudge must fire.
        paths = ["a.py", "b.py", "c.py", "d.py", "e.py"]

        def adapter(messages, tools):
            messages_seen.append(list(messages))
            call_count[0] += 1
            if call_count[0] <= len(paths):
                # Missing required `content` → tool reports an Error result.
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(
                            id=f"c{call_count[0]}",
                            name="write_file",
                            arguments={"path": paths[call_count[0] - 1]},
                        )
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_usage(),
                )
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
        all_msgs = [m for msgs in messages_seen for m in msgs]
        nudges = [
            m
            for m in all_msgs
            if m.get("role") == "user" and "no file modifications" in m.get("content", "")
        ]
        assert len(nudges) >= 1, "failed write_file calls should count as no-progress"


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
