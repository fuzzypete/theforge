"""Tests for the API agent loop in runner_api.py."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.config import ModelProfile
from theforge.runner import ModelUsage
from theforge.runner_api import (
    SUBMIT_PLAN_REVIEW,
    SUBMIT_REVIEW,
    AgentLoopManager,
    LoopTurn,
    ToolCallRequest,
    run_api_agent,
)
from theforge.tool_runtime import TOOL_REGISTRY


def _make_profile(
    name: str = "test-reviewer",
    provider: str = "openai",
    model: str = "gpt-4o",
    allowed_tools: tuple[str, ...] = (),
    timeout_seconds: int = 300,
    max_tool_output_bytes: int = 51200,
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        cli=None,
        model=model,
        budget_usd=1.0,
        timeout_seconds=timeout_seconds,
        allowed_tools=allowed_tools,
        max_tool_output_bytes=max_tool_output_bytes,
    )


def _make_usage(model: str = "gpt-4o") -> ModelUsage:
    return ModelUsage(
        model=model,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=None,
    )


class TestEmptyAllowedToolsSingleShot:
    """When allowed_tools is empty, run_api_agent skips the loop."""

    def test_empty_tools_calls_single_shot_runner(self, tmp_path):
        profile = _make_profile(allowed_tools=())
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = 0.01

        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict("theforge.runner_api.PROVIDER_RUNNERS", {"openai": mock_fn}):
            run_api_agent(
                prompt="review this",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )
            mock_fn.assert_called_once()


class TestAgentLoopLifecycle:
    """Full lifecycle tests with mocked provider adapters."""

    def _make_manager(
        self,
        tmp_path: Path,
        adapter,
        tools=None,
        timeout_seconds: int = 300,
    ) -> AgentLoopManager:
        profile = _make_profile(timeout_seconds=timeout_seconds)
        return AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()) if tools is None else tools,
            provider_adapter=adapter,
        )

    def test_tool_call_then_final_response(self, tmp_path):
        """Mock model calls read_file once, then returns final text."""
        (tmp_path / "foo.py").write_text("def hello(): pass\n", encoding="utf-8")
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="read_file", arguments={"path": "foo.py"})
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[],
                text_output="All looks good.",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "review"}],
            tool_schemas=[],
        )
        assert result.success
        assert result.output == "All looks good."
        assert call_count[0] == 2

    def test_submit_review_tool_extracts_structured_data(self, tmp_path):
        """Model calls submit_review — loop returns structured_data."""
        review_data = {
            "verdict": "APPROVE",
            "summary": "Looks good",
            "findings": [],
            "spec_compliance": {"matches_spec": True, "mismatches": []},
            "test_coverage": {"adequate": True, "gaps": []},
        }

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[ToolCallRequest(id="s1", name=SUBMIT_REVIEW, arguments=review_data)],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "review"}],
            tool_schemas=[],
        )
        assert result.success
        assert result.structured_data == review_data
        assert result.structured_data["verdict"] == "APPROVE"

    def test_submit_plan_review_tool_extracts_structured_data(self, tmp_path):
        """Model calls submit_plan_review — loop returns structured_data."""
        plan_review_data = {
            "verdict": "REQUEST_CHANGES",
            "summary": "Missing tests",
            "findings": [{"severity": "P1", "description": "No tests for X"}],
        }

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(id="p1", name=SUBMIT_PLAN_REVIEW, arguments=plan_review_data)
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "review plan"}],
            tool_schemas=[],
        )
        assert result.success
        assert result.structured_data["verdict"] == "REQUEST_CHANGES"

    def test_token_accumulation_across_turns(self, tmp_path):
        """All four token fields are summed across iterations."""
        turns = [
            LoopTurn(
                tool_calls=[
                    ToolCallRequest(id="c1", name="read_file", arguments={"path": "f.py"})
                ],
                text_output=None,
                structured_data=None,
                usage=ModelUsage(
                    model="gpt-4o",
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_tokens=10,
                    cache_creation_tokens=5,
                    cost_usd=None,
                ),
            ),
            LoopTurn(
                tool_calls=[],
                text_output="done",
                structured_data=None,
                usage=ModelUsage(
                    model="gpt-4o",
                    input_tokens=200,
                    output_tokens=80,
                    cache_read_tokens=20,
                    cache_creation_tokens=15,
                    cost_usd=None,
                ),
            ),
        ]
        # Create the file so read_file doesn't error
        (tmp_path / "f.py").write_text("x", encoding="utf-8")

        turn_iter = iter(turns)

        def adapter(messages, tools):
            return next(turn_iter)

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        assert len(result.model_usage) == 1
        usage = result.model_usage[0]
        assert usage.input_tokens == 300
        assert usage.output_tokens == 130
        assert usage.cache_read_tokens == 30
        assert usage.cache_creation_tokens == 20

    def test_max_iterations_terminates_loop_with_accumulated_cost(self, tmp_path):
        """Loop terminates after max_iterations, still reports cost."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id=f"c{call_count[0]}", name="glob", arguments={"pattern": "*.py"}
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        profile = _make_profile(timeout_seconds=300)
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
            max_iterations=3,
        )
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        assert "max iterations" in result.output
        assert result.model_usage  # cost still reported
        assert call_count[0] == 3

    def test_timeout_terminates_loop_with_accumulated_cost(self, tmp_path):
        """Wall-clock timeout terminates loop, cost is still reported."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id=f"c{call_count[0]}", name="glob", arguments={"pattern": "*.py"}
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        profile = _make_profile(timeout_seconds=1)
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
        )
        # Fast-forward time past deadline
        past_deadline = time.monotonic() + 999
        with patch("theforge.runner_api.time") as mock_time:
            mock_time.monotonic.return_value = past_deadline
            result = manager.run(
                initial_messages=[{"role": "user", "content": "go"}],
                tool_schemas=[],
            )
        assert not result.success
        assert result.model_usage  # cost still reported even on timeout

    def test_tool_error_fed_back_as_result_loop_continues(self, tmp_path):
        """Tool execution error is returned as text result; loop continues."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                # Call read_file on nonexistent file
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="read_file", arguments={"path": "nope.py"})
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            # Second call: model got error, now returns text
            return LoopTurn(
                tool_calls=[],
                text_output="OK, file does not exist.",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        # Loop should continue and succeed on second call
        assert result.success
        # Verify tool error was fed back (second message has role tool_results)
        assert call_count[0] == 2

    def test_unknown_tool_name_returns_error_loop_continues(self, tmp_path):
        """Unknown tool name returns error to model; loop continues."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="list_files", arguments={"dir": "."})
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[],
                text_output="Sorry, will use glob instead.",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        assert call_count[0] == 2

    def test_malformed_arguments_return_error_loop_continues(self, tmp_path):
        """Malformed (non-dict) arguments return error; loop continues."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                # Return a malformed call (None name triggers malformed path)
                return LoopTurn(
                    tool_calls=[ToolCallRequest(id="c1", name="", arguments={})],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[],
                text_output="recovered",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        # One malformed call should not abort
        assert call_count[0] == 2

    def test_three_consecutive_malformed_aborts_loop(self, tmp_path):
        """3 consecutive malformed calls abort the loop."""

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[ToolCallRequest(id="cx", name="", arguments={})],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        assert "malformed" in result.output

    def test_parallel_tool_execution(self, tmp_path):
        """Multiple tool calls in one turn are executed in parallel."""
        (tmp_path / "a.py").write_text("hello", encoding="utf-8")
        (tmp_path / "b.py").write_text("world", encoding="utf-8")

        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                # Two tool calls in one turn
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"}),
                        ToolCallRequest(id="c2", name="read_file", arguments={"path": "b.py"}),
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[],
                text_output="both files read",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        # Check tool results were passed back (second call messages include both results)
        assert call_count[0] == 2

    def test_tool_results_fed_back_in_messages(self, tmp_path):
        """Tool results appear in subsequent messages to the model."""
        (tmp_path / "check.py").write_text("answer = 42\n", encoding="utf-8")
        received_messages = []

        def adapter(messages, tools):
            received_messages.append(list(messages))
            if len(received_messages) == 1:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(id="c1", name="read_file", arguments={"path": "check.py"})
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[],
                text_output="I see answer = 42",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        # Second call should have 3 messages: user, assistant w/ tool_calls, tool_results
        second_call_msgs = received_messages[1]
        assert len(second_call_msgs) == 3
        roles = [m["role"] for m in second_call_msgs]
        assert roles == ["user", "assistant", "tool_results"]
        # Tool result should contain the file contents
        tool_result_msg = second_call_msgs[2]
        assert "answer = 42" in tool_result_msg["results"][0]["content"]


class TestRunApiAgentLoopIntegration:
    """Integration tests for run_api_agent with loop path."""

    def test_run_api_agent_with_tools_calls_loop_runner(self, tmp_path):
        profile = _make_profile(
            provider="openai",
            model="gpt-4o",
            allowed_tools=("read_file",),
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = None

        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict("theforge.runner_api._LOOP_RUNNERS", {"openai": mock_fn}):
            run_api_agent(
                prompt="review",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )
            mock_fn.assert_called_once_with("review", profile, tmp_path, None)

    def test_run_api_agent_no_provider_returns_failure(self, tmp_path):
        profile = ModelProfile(
            name="no-provider",
            provider=None,
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        result = run_api_agent(
            prompt="review",
            profile=profile,
            working_dir=tmp_path,
            quiet=True,
        )
        assert not result.success
        assert "not an API profile" in result.output
