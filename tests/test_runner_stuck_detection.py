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
