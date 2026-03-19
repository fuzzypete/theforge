"""Tests for the API agent loop in runner_api.py."""

from __future__ import annotations

import json
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
    _deepseek_client,
    _is_reasoning_model,
    _make_google_adapter,
    _run_deepseek,
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

    def test_profile_max_iterations_overrides_default(self, tmp_path):
        """ModelProfile.max_iterations takes precedence over constructor default."""
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
        # Override max_iterations on the profile itself
        profile = ModelProfile(
            name=profile.name,
            provider=profile.provider,
            cli=profile.cli,
            model=profile.model,
            budget_usd=profile.budget_usd,
            timeout_seconds=profile.timeout_seconds,
            allowed_tools=profile.allowed_tools,
            max_iterations=5,
        )
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
            max_iterations=3,  # would be 3 without profile override
        )
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        # Profile's max_iterations=5 should win over constructor's max_iterations=3
        assert call_count[0] == 5

    def test_nudge_injected_near_iteration_limit(self, tmp_path):
        """A wrap-up nudge message is injected at ~80% of the iteration budget."""
        messages_seen: list[list[dict]] = []

        def adapter(messages, tools):
            messages_seen.append(list(messages))
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id=f"c{len(messages_seen)}", name="glob", arguments={"pattern": "*.py"}
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
            max_iterations=5,  # nudge at iteration 4 (80% of 5)
        )
        manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        # After iteration 4, a nudge user message should be injected
        # The 5th call's messages should contain the nudge
        last_messages = messages_seen[-1]
        nudge_msgs = [
            m
            for m in last_messages
            if m.get("role") == "user" and "iterations remaining" in m.get("content", "")
        ]
        assert len(nudge_msgs) == 1, f"Expected exactly 1 nudge, got {len(nudge_msgs)}"
        assert "submit" in nudge_msgs[0]["content"].lower()

    def test_nudge_not_sent_when_submit_before_threshold(self, tmp_path):
        """No nudge if the model submits before reaching 80% of iterations."""
        messages_seen: list[list[dict]] = []
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            messages_seen.append(list(messages))
            if call_count[0] == 2:
                # Submit on second iteration (well before 80% of 10)
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(
                            id="submit1",
                            name=SUBMIT_REVIEW,
                            arguments={"verdict": "APPROVE", "summary": "lgtm"},
                        )
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
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
            max_iterations=10,
        )
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        # No nudge should have been sent
        for msgs in messages_seen:
            nudge_msgs = [
                m
                for m in msgs
                if m.get("role") == "user" and "iterations remaining" in m.get("content", "")
            ]
            assert len(nudge_msgs) == 0

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

    def test_mixed_malformed_and_valid_calls_in_same_turn(self, tmp_path):
        """Mixed malformed and valid calls: error for bad, execute good, one history entry."""
        (tmp_path / "real.py").write_text("content", encoding="utf-8")
        received_second_call_messages = []

        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                # One valid call + one malformed call in the same turn
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(
                            id="good", name="read_file", arguments={"path": "real.py"}
                        ),
                        ToolCallRequest(id="bad", name="", arguments={}),
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            received_second_call_messages.extend(messages)
            return LoopTurn(
                tool_calls=[],
                text_output="done",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        # History should be: user, assistant, tool_results (one append_tool_results call)
        assert call_count[0] == 2
        roles = [m["role"] for m in received_second_call_messages]
        assert roles == ["user", "assistant", "tool_results"]
        # Both tool results (good result + error for bad) must be present
        tool_results = received_second_call_messages[2]["results"]
        assert len(tool_results) == 2
        result_ids = {r["id"] for r in tool_results}
        assert result_ids == {"good", "bad"}
        # The bad call result should contain an error message
        bad_result = next(r for r in tool_results if r["id"] == "bad")
        assert "Error" in bad_result["content"]

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

    def test_adapter_type_error_returns_provider_api_error(self, tmp_path):
        """Adapter raising TypeError('NoneType' object is not iterable) → failure result."""

        def adapter(messages, tools):
            raise TypeError("'NoneType' object is not iterable")

        manager = self._make_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        assert "Provider API error" in result.output
        assert "'NoneType' object is not iterable" in result.output

    def _make_google_modules(self, fake_client):
        """Build sys.modules mocks for google.genai, wiring attributes correctly.

        `import google.genai as genai` resolves via attribute access on the google
        module object, not directly from sys.modules["google.genai"], so we must set
        mock_google.genai = mock_genai and mock_genai.types = mock_genai_types.
        """
        import sys

        mock_genai_types = MagicMock()
        mock_genai = MagicMock()
        mock_genai.Client.return_value = fake_client
        mock_genai.types = mock_genai_types

        mock_google = MagicMock()
        mock_google.genai = mock_genai

        modules = {
            "google": mock_google,
            "google.genai": mock_genai,
            "google.genai.types": mock_genai_types,
        }
        return sys.modules, modules, mock_genai, mock_genai_types

    def test_google_adapter_fc_args_none_parsed_as_empty_dict(self, tmp_path):
        """Google adapter with fc.args = None should produce arguments={}."""
        # Build a fake response where a function_call part has args=None
        fake_fc = MagicMock()
        fake_fc.name = "submit_review"
        fake_fc.args = None

        fake_part = MagicMock()
        fake_part.function_call = fake_fc
        fake_part.text = None

        fake_candidate = MagicMock()
        fake_candidate.content = MagicMock()
        fake_candidate.content.parts = [fake_part]

        fake_response = MagicMock()
        fake_response.candidates = [fake_candidate]
        fake_response.usage_metadata = None
        fake_response.prompt_feedback = None

        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response

        import sys

        _, modules, _, _ = self._make_google_modules(fake_client)
        profile = _make_profile(provider="google", model="gemini-2.5-flash")

        with patch.dict(sys.modules, modules):
            adapter = _make_google_adapter(profile, secrets=None)
            turn = adapter([{"role": "user", "content": "hi"}], [])

        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].arguments == {}

    def test_google_adapter_content_parts_none_yields_empty(self, tmp_path):
        """Google adapter with candidate.content.parts = None → no tool_calls, no text."""
        fake_candidate = MagicMock()
        fake_candidate.content = MagicMock()
        fake_candidate.content.parts = None  # parts is None, not []

        fake_response = MagicMock()
        fake_response.candidates = [fake_candidate]
        fake_response.usage_metadata = None
        fake_response.prompt_feedback = None

        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response

        import sys

        _, modules, _, _ = self._make_google_modules(fake_client)
        profile = _make_profile(provider="google", model="gemini-2.5-flash")

        with patch.dict(sys.modules, modules):
            adapter = _make_google_adapter(profile, secrets=None)
            turn = adapter([{"role": "user", "content": "hi"}], [])

        assert turn.tool_calls == []
        assert turn.text_output is None


class TestTimeNudge:
    """Tests for wall-clock time-based nudge."""

    def test_time_nudge_sent_at_80_percent_deadline(self, tmp_path):
        """Time-based nudge is injected when 80% of wall-clock has elapsed."""
        messages_seen = []
        call_count = [0]
        # Use a controllable clock: start at 1000, timeout=100s, deadline=1100
        # 80% threshold = 80s elapsed = time 1080
        current_time = [1000.0]

        def adapter(messages, tools):
            messages_seen.append(list(messages))
            call_count[0] += 1
            # Advance time by 12s per iteration
            current_time[0] += 12.0
            if call_count[0] <= 8:
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
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="submit1",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        with patch("theforge.runner_api.time") as mock_time:
            mock_time.monotonic = lambda: current_time[0]
            profile = _make_profile(timeout_seconds=100)
            manager = AgentLoopManager(
                profile=profile,
                provider="openai",
                working_dir=tmp_path,
                tools=list(TOOL_REGISTRY.values()),
                provider_adapter=adapter,
                max_iterations=50,
            )
            result = manager.run(
                initial_messages=[{"role": "user", "content": "go"}],
                tool_schemas=[],
            )

        assert result.success
        # Check that a time nudge message was sent (it persists in subsequent calls,
        # so count unique nudge contents — should be exactly 1 unique nudge)
        all_messages = [m for msgs in messages_seen for m in msgs]
        time_nudges = [
            m.get("content", "")
            for m in all_messages
            if m.get("role") == "user" and "seconds remaining" in m.get("content", "")
        ]
        assert len(time_nudges) >= 1
        # All nudge messages should be identical (sent once, seen in subsequent turns)
        assert len(set(time_nudges)) == 1


class TestFinalization:
    """Tests for forced-output finalization on timeout."""

    def test_finalization_on_wall_clock_timeout(self, tmp_path):
        """When wall-clock times out, finalizer is called and returns structured data."""
        review_data = {
            "verdict": "APPROVE",
            "summary": "Finalized review",
            "findings": [],
            "spec_compliance": {"matches_spec": True, "mismatches": []},
            "test_coverage": {"adequate": True, "gaps": []},
        }

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[ToolCallRequest(id="c1", name="glob", arguments={"pattern": "*.py"})],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        def finalizer(messages):
            return LoopTurn(
                tool_calls=[],
                text_output=json.dumps(review_data),
                structured_data=review_data,
                usage=_make_usage(),
            )

        profile = _make_profile(timeout_seconds=1)
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
            finalizer=finalizer,
        )
        # Force immediate timeout
        past_deadline = time.monotonic() + 999
        with patch("theforge.runner_api.time") as mock_time:
            mock_time.monotonic.return_value = past_deadline
            result = manager.run(
                initial_messages=[{"role": "user", "content": "go"}],
                tool_schemas=[],
            )
        assert result.success
        assert result.structured_data == review_data
        assert result.structured_data["verdict"] == "APPROVE"

    def test_finalization_on_max_iterations(self, tmp_path):
        """When max iterations exceeded, finalizer is called."""
        review_data = {"verdict": "APPROVE", "summary": "From finalizer", "findings": []}

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

        def finalizer(messages):
            return LoopTurn(
                tool_calls=[],
                text_output=json.dumps(review_data),
                structured_data=review_data,
                usage=_make_usage(),
            )

        profile = _make_profile(timeout_seconds=300)
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
            finalizer=finalizer,
            max_iterations=3,
        )
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        assert result.structured_data["summary"] == "From finalizer"
        assert call_count[0] == 3

    def test_finalization_failure_falls_back_to_timeout(self, tmp_path):
        """When finalizer raises, fall back to normal timeout failure."""

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[ToolCallRequest(id="c1", name="glob", arguments={"pattern": "*.py"})],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        def finalizer(messages):
            raise RuntimeError("API error during finalization")

        profile = _make_profile(timeout_seconds=300)
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
            finalizer=finalizer,
            max_iterations=2,
        )
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        assert "max iterations" in result.output

    def test_no_finalizer_returns_timeout_failure(self, tmp_path):
        """Without a finalizer, timeout returns failure as before."""

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[ToolCallRequest(id="c1", name="glob", arguments={"pattern": "*.py"})],
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
            # No finalizer
            max_iterations=2,
        )
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        assert "max iterations" in result.output

    def test_finalization_with_text_only_parses_json(self, tmp_path):
        """Finalizer returning text_output (no structured_data) still parses JSON."""
        review_data = {
            "verdict": "REQUEST_CHANGES",
            "summary": "Needs work",
            "findings": [{"severity": "P1", "file": "x.py", "description": "bug"}],
        }

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[ToolCallRequest(id="c1", name="glob", arguments={"pattern": "*.py"})],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        def finalizer(messages):
            # Returns text but no structured_data — should be parsed
            return LoopTurn(
                tool_calls=[],
                text_output=json.dumps(review_data),
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
            finalizer=finalizer,
            max_iterations=2,
        )
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        assert result.structured_data["verdict"] == "REQUEST_CHANGES"


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


class TestRateLimitRetry:
    """Tests for AgentLoopManager._call_with_retry."""

    def _make_manager(self, tmp_path, timeout_seconds: int = 300) -> AgentLoopManager:
        profile = _make_profile(timeout_seconds=timeout_seconds)
        # Adapter is replaced per-test
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=[],
            provider_adapter=lambda m, t: (_ for _ in ()).throw(RuntimeError("unreachable")),
        )
        return manager

    def test_retry_succeeds_after_one_rate_limit(self, tmp_path):
        """First call raises 429, second call succeeds."""
        call_count = [0]
        good_turn = LoopTurn(tool_calls=[], text_output="ok", structured_data=None, usage=None)

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("429 Rate limit reached")
            return good_turn

        manager = self._make_manager(tmp_path)
        manager._adapter = adapter

        with patch("theforge.runner_api.time") as mock_time:
            # monotonic values: first check not timed out, remaining check, second check
            mock_time.monotonic.side_effect = [
                manager._deadline - 200,  # check 1: not timed out
                manager._deadline - 200,  # remaining check in retry
                manager._deadline - 150,  # check 2 inside run loop
            ]
            mock_time.sleep = MagicMock()
            result = manager._call_with_retry([{"role": "user", "content": "hi"}], [])

        assert result is good_turn
        assert call_count[0] == 2
        mock_time.sleep.assert_called_once_with(30)  # first backoff = 30s

    def test_non_rate_limit_error_propagates_immediately(self, tmp_path):
        """Non-429 exceptions are re-raised without retry."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            raise ValueError("something else broke")

        manager = self._make_manager(tmp_path)
        manager._adapter = adapter

        import pytest

        with pytest.raises(ValueError, match="something else broke"):
            manager._call_with_retry([{"role": "user", "content": "hi"}], [])

        assert call_count[0] == 1  # no retry

    def test_gives_up_when_deadline_too_close(self, tmp_path):
        """If remaining time < backoff wait, raises immediately without sleeping."""
        import pytest

        def adapter(messages, tools):
            raise RuntimeError("429 quota exceeded")

        manager = self._make_manager(tmp_path, timeout_seconds=10)
        manager._adapter = adapter

        # Make deadline appear very close (5s left, first backoff = 30s)
        near_deadline = manager._deadline - 5

        with patch("theforge.runner_api.time") as mock_time:
            mock_time.monotonic.return_value = near_deadline
            mock_time.sleep = MagicMock()
            with pytest.raises(RuntimeError, match="429"):
                manager._call_with_retry([{"role": "user", "content": "hi"}], [])

        mock_time.sleep.assert_not_called()

    def test_exhausts_all_retries_then_raises(self, tmp_path):
        """After _MAX_RATE_LIMIT_RETRIES retries, raises the last exception."""
        import pytest

        from theforge.runner_api import _MAX_RATE_LIMIT_RETRIES

        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            raise RuntimeError("429 rate limit every time")

        manager = self._make_manager(tmp_path, timeout_seconds=9999)
        manager._adapter = adapter

        with patch("theforge.runner_api.time") as mock_time:
            mock_time.monotonic.return_value = manager._deadline - 9000
            mock_time.sleep = MagicMock()
            with pytest.raises(RuntimeError, match="429"):
                manager._call_with_retry([{"role": "user", "content": "hi"}], [])

        assert call_count[0] == _MAX_RATE_LIMIT_RETRIES + 1

    def test_run_loop_returns_failure_on_non_retryable_error(self, tmp_path):
        """run() wraps adapter exceptions as failure results."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            raise ConnectionError("network gone")

        manager = self._make_manager(tmp_path)
        manager._adapter = adapter

        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert not result.success
        assert "Provider API error" in result.output
        assert call_count[0] == 1


class TestDeepSeekProvider:
    """Tests for DeepSeek provider wiring."""

    def _make_deepseek_profile(
        self,
        model: str = "deepseek-r1",
        base_url: str | None = None,
        allowed_tools: tuple[str, ...] = (),
    ) -> ModelProfile:
        return ModelProfile(
            name="deepseek-reviewer",
            provider="deepseek",
            cli=None,
            model=model,
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=allowed_tools,
            base_url=base_url,
        )

    # ── _is_reasoning_model ──────────────────────────────────────────

    def test_is_reasoning_model_deepseek_r1(self):
        assert _is_reasoning_model("deepseek-r1") is True

    def test_is_reasoning_model_deepseek_r1_prefix_variants(self):
        assert _is_reasoning_model("deepseek-r1-zero") is True
        assert _is_reasoning_model("deepseek-r1-distill-qwen-7b") is True

    def test_is_reasoning_model_deepseek_v3_is_false(self):
        assert _is_reasoning_model("deepseek-v3") is False

    # ── _deepseek_client base_url ─────────────────────────────────────

    def _patch_openai(self):
        """Return a context manager that stubs openai and httpx modules for _deepseek_client."""
        import sys

        mock_openai = MagicMock()
        mock_openai.OpenAI = MagicMock()
        mock_httpx = MagicMock()
        return patch.dict(sys.modules, {"openai": mock_openai, "httpx": mock_httpx}), mock_openai

    def test_deepseek_client_default_base_url(self):
        profile = self._make_deepseek_profile()
        mod_patch, mock_module = self._patch_openai()
        with (
            patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}),
            mod_patch,
        ):
            _deepseek_client(profile, {})
        _, kwargs = mock_module.OpenAI.call_args
        assert kwargs["base_url"] == "https://api.deepseek.com"

    def test_deepseek_client_respects_profile_base_url(self):
        profile = self._make_deepseek_profile(base_url="http://localhost:11434")
        mod_patch, mock_module = self._patch_openai()
        with mod_patch:
            _deepseek_client(profile, {})
        _, kwargs = mock_module.OpenAI.call_args
        assert kwargs["base_url"] == "http://localhost:11434"

    def test_deepseek_client_uses_deepseek_api_key(self):
        profile = self._make_deepseek_profile()
        mod_patch, mock_module = self._patch_openai()
        with (
            patch.dict("os.environ", {}, clear=True),
            mod_patch,
        ):
            _deepseek_client(profile, {"DEEPSEEK_API_KEY": "sk-secrets"})
        _, kwargs = mock_module.OpenAI.call_args
        assert kwargs["api_key"] == "sk-secrets"

    # ── _run_deepseek single-shot ─────────────────────────────────────

    def _mock_review_response(self, mock_client: MagicMock) -> MagicMock:
        """Wire mock_client to return a minimal valid review JSON."""
        mock_response = MagicMock()
        mock_response.choices[0].message.content = json.dumps(
            {
                "verdict": "APPROVE",
                "summary": "ok",
                "findings": [],
                "spec_compliance": {"matches_spec": True, "mismatches": []},
                "test_coverage": {"adequate": True, "gaps": []},
            }
        )
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.model_dump.return_value = {}
        mock_client.chat.completions.create.return_value = mock_response
        return mock_response

    def test_run_deepseek_calls_chat_completions(self, tmp_path):
        profile = self._make_deepseek_profile(model="deepseek-v3")
        mock_client = MagicMock()
        self._mock_review_response(mock_client)

        with patch("theforge.runner_api._deepseek_client", return_value=mock_client):
            result = _run_deepseek("review this", profile)

        assert result.success
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "deepseek-v3"
        # deepseek-v3 is not a reasoning model — temperature should be set
        assert call_kwargs.get("temperature") == 0

    def test_run_deepseek_r1_skips_temperature(self, tmp_path):
        profile = self._make_deepseek_profile(model="deepseek-r1")
        mock_client = MagicMock()
        self._mock_review_response(mock_client)

        with patch("theforge.runner_api._deepseek_client", return_value=mock_client):
            result = _run_deepseek("review this", profile)

        assert result.success
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert "temperature" not in call_kwargs

    # ── _run_loop_deepseek dispatch ───────────────────────────────────

    def test_run_api_agent_deepseek_single_shot_dispatches(self, tmp_path):
        profile = self._make_deepseek_profile(allowed_tools=())
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = None
        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict("theforge.runner_api.PROVIDER_RUNNERS", {"deepseek": mock_fn}):
            run_api_agent(prompt="review", profile=profile, working_dir=tmp_path, quiet=True)
        mock_fn.assert_called_once()

    def test_run_api_agent_deepseek_loop_dispatches(self, tmp_path):
        profile = self._make_deepseek_profile(allowed_tools=("read_file",))
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = None
        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict("theforge.runner_api._LOOP_RUNNERS", {"deepseek": mock_fn}):
            run_api_agent(prompt="review", profile=profile, working_dir=tmp_path, quiet=True)
        mock_fn.assert_called_once_with("review", profile, tmp_path, None)
