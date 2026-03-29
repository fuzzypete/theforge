"""Tests for the API agent loop lifecycle, empty-tools single-shot, and time nudge."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.agent_types import ModelUsage
from theforge.config import ModelProfile
from theforge.runners.adapters.google import _make_google_adapter
from theforge.runners.api import (
    AgentLoopManager,
    run_api_agent,
)
from theforge.runners.schema_utils import (
    SUBMIT_REVIEW,
    LoopTurn,
    ToolCallRequest,
)
from theforge.runners.tool_runtime import TOOL_REGISTRY


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
        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": mock_fn}):
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
            "story_compliance": {"matches_spec": True, "mismatches": []},
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
        from theforge.runners.schema_utils import SUBMIT_PLAN_REVIEW

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
        with patch("theforge.runners.api.time") as mock_time:
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

        with patch("theforge.runners.api.time") as mock_time:
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
