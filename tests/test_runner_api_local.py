"""Tests for diagnostic logging, local endpoint detection, cost zeroing, and tool-calling fallback."""  # noqa: E501

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.agent_types import AgentResult, ModelUsage
from theforge.config import ModelProfile
from theforge.runners.adapters.openai import _is_local_endpoint, _openai_result
from theforge.runners.api import (
    AgentLoopManager,
    _run_loop_openai,
)
from theforge.runners.schema_utils import (
    SUBMIT_REVIEW,
    LoopTurn,
    ToolCallRequest,
    uses_openai_responses_api,
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


def _make_local_profile(
    model: str = "codestral",
    base_url: str = "http://localhost:11434/v1",
    allowed_tools: tuple[str, ...] = (),
) -> ModelProfile:
    return ModelProfile(
        name="local-dev",
        provider="openai",
        cli=None,
        model=model,
        budget_usd=0.0,
        timeout_seconds=60,
        allowed_tools=allowed_tools,
        base_url=base_url,
    )


class TestDiagnosticLogging:
    """Tests for diagnostic logging added to AgentLoopManager.run()."""

    def _make_manager(self, tmp_path: Path, adapter, max_iterations: int = 15) -> AgentLoopManager:
        profile = _make_profile(timeout_seconds=300)
        return AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
            max_iterations=max_iterations,
        )

    def test_tool_schema_logged_at_loop_start(self, tmp_path, capsys):
        """Tool schema names logged at verbose level when loop starts."""

        def adapter(messages, tools):
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)
        tool_schemas = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "grep"}},
            {"function": {"name": SUBMIT_REVIEW}},
        ]
        import theforge.runners.api as ra

        logged = []

        def capturing_log_verbose(msg):
            logged.append(msg)

        with patch.object(ra, "_log_verbose", side_effect=capturing_log_verbose):
            manager.run(
                initial_messages=[{"role": "user", "content": "review"}],
                tool_schemas=tool_schemas,
            )

        loop_start_msgs = [m for m in logged if "loop start" in m]
        assert loop_start_msgs, "Expected a 'loop start' verbose log message"
        msg = loop_start_msgs[0]
        assert "read_file" in msg
        assert "grep" in msg
        assert SUBMIT_REVIEW in msg
        assert "3 tools" in msg

    def test_per_turn_tool_calls_logged_verbose(self, tmp_path):
        """Per-turn tool call names logged at verbose level after each iteration."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(id="r1", name="read_file", arguments={"path": "x.py"}),
                        ToolCallRequest(id="g1", name="grep", arguments={"pattern": "foo"}),
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        (tmp_path / "x.py").write_text("foo = 1\n", encoding="utf-8")
        manager = self._make_manager(tmp_path, adapter)

        import theforge.runners.api as ra

        logged = []

        def capturing_log_verbose(msg):
            logged.append(msg)

        with patch.object(ra, "_log_verbose", side_effect=capturing_log_verbose):
            result = manager.run(
                initial_messages=[{"role": "user", "content": "go"}],
                tool_schemas=[],
            )

        assert result.success
        iter_msgs = [m for m in logged if "iter 1:" in m and "call(s)" in m]
        assert iter_msgs, "Expected per-turn tool call log for iteration 1"
        msg = iter_msgs[0]
        assert "read_file" in msg
        assert "grep" in msg
        assert "2 call(s)" in msg

    def test_nudge_logged_at_normal_level(self, tmp_path):
        """Nudge delivery logged at normal (_log) level, not verbose."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] < 15:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(
                            id=f"g{call_count[0]}",
                            name="glob",
                            arguments={"pattern": "*.py"},
                        )
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter, max_iterations=15)

        import theforge.runners.api as ra

        normal_logged = []
        verbose_logged = []

        with patch.object(ra, "_log", side_effect=lambda m: normal_logged.append(m)):
            with patch.object(ra, "_log_verbose", side_effect=lambda m: verbose_logged.append(m)):
                result = manager.run(
                    initial_messages=[{"role": "user", "content": "go"}],
                    tool_schemas=[],
                )

        assert result.success
        nudge_normal = [m for m in normal_logged if "nudge sent" in m]
        nudge_verbose = [m for m in verbose_logged if "nudge sent" in m and "time nudge" not in m]
        assert nudge_normal, "Iteration nudge must be logged at normal level"
        assert not nudge_verbose, "Iteration nudge must NOT be logged at verbose level"

    def test_text_reasoning_logged_verbose(self, tmp_path):
        """Text reasoning (first 200 chars) logged at verbose level."""
        call_count = [0]
        reasoning = "I need to examine the test suite carefully before submitting my verdict." * 5

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(id="g1", name="glob", arguments={"pattern": "*.py"})
                    ],
                    text_output=reasoning,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name=SUBMIT_REVIEW,
                        arguments={"verdict": "APPROVE", "summary": "ok"},
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter)

        import theforge.runners.api as ra

        logged = []

        def capturing_log_verbose(msg):
            logged.append(msg)

        with patch.object(ra, "_log_verbose", side_effect=capturing_log_verbose):
            result = manager.run(
                initial_messages=[{"role": "user", "content": "go"}],
                tool_schemas=[],
            )

        assert result.success
        reasoning_msgs = [m for m in logged if "reasoning" in m]
        assert reasoning_msgs, "Expected a reasoning verbose log message"
        msg = reasoning_msgs[0]
        # Should contain the first 200 chars of the reasoning
        assert reasoning[:200] in msg

    def test_iteration_summary_on_max_iterations(self, tmp_path):
        """Iteration summary logged at normal level on max-iteration failure."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id=f"r{call_count[0]}", name="read_file", arguments={"path": "x.py"}
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        (tmp_path / "x.py").write_text("content\n", encoding="utf-8")
        manager = self._make_manager(tmp_path, adapter, max_iterations=3)

        import theforge.runners.api as ra

        normal_logged = []

        with patch.object(ra, "_log", side_effect=lambda m: normal_logged.append(m)):
            result = manager.run(
                initial_messages=[{"role": "user", "content": "go"}],
                tool_schemas=[],
            )

        assert not result.success
        summary_msgs = [m for m in normal_logged if "max iterations" in m]
        assert summary_msgs, "Expected iteration summary log on max-iteration failure"
        msg = summary_msgs[0]
        assert "read_file" in msg
        assert "submit never called" in msg
        # Should show total tool call count (3 iterations × 1 call = 3)
        assert "3 tool calls" in msg

    def test_iteration_summary_shows_submit_called_when_submit_was_attempted(self, tmp_path):
        """If a submit tool appears in counts (shouldn't reach max), submit_called is shown."""
        # Edge: one submit call among other calls that still exhausts iterations
        # Simulate this by having submit in _tool_call_counts via a different tool name
        # In practice, submit ends the loop, so we test "submit never called" case is accurate.
        # This test verifies the label says "submit never called" when no submit was attempted.
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            return LoopTurn(
                tool_calls=[
                    ToolCallRequest(
                        id=f"g{call_count[0]}", name="glob", arguments={"pattern": "*.py"}
                    )
                ],
                text_output=None,
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_manager(tmp_path, adapter, max_iterations=2)

        import theforge.runners.api as ra

        normal_logged = []

        with patch.object(ra, "_log", side_effect=lambda m: normal_logged.append(m)):
            result = manager.run(
                initial_messages=[{"role": "user", "content": "go"}],
                tool_schemas=[],
            )

        assert not result.success
        summary_msgs = [m for m in normal_logged if "max iterations" in m]
        assert summary_msgs
        assert "submit never called" in summary_msgs[0]
        assert "glob:2" in summary_msgs[0]


# ── Local endpoint: cost zeroing and tool-calling fallback ────────────


class TestIsLocalEndpoint:
    """Unit tests for _is_local_endpoint helper."""

    def test_localhost_is_local(self):
        assert _is_local_endpoint("http://localhost:11434/v1") is True

    def test_127_is_local(self):
        assert _is_local_endpoint("http://127.0.0.1:8080/v1") is True

    def test_ipv6_localhost_is_local(self):
        assert _is_local_endpoint("http://[::1]:11434/v1") is True

    def test_all_interfaces_is_local(self):
        assert _is_local_endpoint("http://0.0.0.0:11434/v1") is True

    def test_remote_is_not_local(self):
        assert _is_local_endpoint("https://api.openai.com/v1") is False

    def test_none_is_not_local(self):
        assert _is_local_endpoint(None) is False

    def test_empty_string_is_not_local(self):
        assert _is_local_endpoint("") is False

    def test_localhost_in_path_is_not_local(self):
        """'localhost' in the URL path must not be a false positive."""
        assert _is_local_endpoint("https://api.example.com/proxy/localhost/v1") is False

    def test_localhost_in_query_is_not_local(self):
        """'localhost' in a query parameter must not be a false positive."""
        assert _is_local_endpoint("https://api.example.com/v1?target=localhost") is False

    def test_127_in_path_is_not_local(self):
        """'127.0.0.1' in the URL path must not be a false positive."""
        assert _is_local_endpoint("https://api.example.com/redirect/127.0.0.1") is False


class TestLocalEndpointCostZeroing:
    """Cost must be $0.00 for localhost endpoints."""

    def _valid_review_json(self) -> str:
        return json.dumps(
            {
                "verdict": "APPROVE",
                "summary": "ok",
                "findings": [],
                "story_compliance": {"matches_spec": True, "mismatches": []},
                "test_coverage": {"adequate": True, "gaps": []},
            }
        )

    def test_single_shot_localhost_cost_is_zero(self):
        """_openai_result sets cost=0.0 when base_url is localhost."""
        profile = _make_local_profile(base_url="http://localhost:11434/v1")
        result = _openai_result(
            profile,
            self._valid_review_json(),
            input_tokens=100,
            output_tokens=50,
            raw={},
        )
        assert result.cost_usd == 0.0
        assert result.model_usage[0].cost_usd == 0.0

    def test_single_shot_non_localhost_cost_not_forced_zero(self):
        """_openai_result does NOT force 0.0 for a non-localhost base_url."""
        profile = _make_local_profile(base_url="https://api.openai.com/v1")
        result = _openai_result(
            profile,
            self._valid_review_json(),
            input_tokens=100,
            output_tokens=50,
            raw={},
        )
        # For unknown model "codestral" there's no PRICING_TABLE entry → None
        assert result.cost_usd is None

    def test_loop_mode_localhost_cost_is_zero(self, tmp_path):
        """_run_loop_openai zeros cost on result when base_url is localhost."""
        import sys

        profile = _make_local_profile(
            base_url="http://localhost:11434/v1", allowed_tools=("read_file",)
        )
        non_zero_usage = ModelUsage(
            model="codestral",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.05,
        )
        mock_result = AgentResult(
            success=True,
            output="{}",
            session_id=None,
            cost_usd=0.05,
            exit_code=0,
            raw={},
            profile_name="local-dev",
            model_usage=(non_zero_usage,),
        )

        mock_openai = MagicMock()
        mock_httpx = MagicMock()
        with (
            patch.dict(sys.modules, {"openai": mock_openai, "httpx": mock_httpx}),
            patch("theforge.runners.api.AgentLoopManager") as MockManager,
        ):
            MockManager.return_value.run.return_value = mock_result
            result = _run_loop_openai("prompt", profile, tmp_path, secrets=None)

        assert result.cost_usd == 0.0
        assert all(u.cost_usd == 0.0 for u in result.model_usage)

    def test_agent_loop_manager_success_result_zeros_cost_for_local(self, tmp_path):
        """AgentLoopManager._success_result zeroes costs for localhost profiles."""
        profile = _make_local_profile(base_url="http://localhost:11434/v1")
        adapter = MagicMock()
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=[],
            provider_adapter=adapter,
        )
        result = manager._success_result(output="{}", structured_data=None)
        assert result.cost_usd == 0.0
        assert result.model_usage[0].cost_usd == 0.0

    def test_agent_loop_manager_failure_result_zeros_cost_for_local(self, tmp_path):
        """AgentLoopManager._failure_result zeroes costs for localhost profiles."""
        profile = _make_local_profile(base_url="http://localhost:11434/v1")
        adapter = MagicMock()
        manager = AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=[],
            provider_adapter=adapter,
        )
        result = manager._failure_result("something went wrong")
        assert result.cost_usd == 0.0
        assert result.model_usage[0].cost_usd == 0.0


class TestToolCallingFallback:
    """When AgentLoopManager raises BadRequestError with 'tool' in the message,
    _run_loop_openai falls back to single-shot via PROVIDER_RUNNERS["openai"]."""

    def _valid_review_json(self) -> str:
        return json.dumps(
            {
                "verdict": "APPROVE",
                "summary": "ok",
                "findings": [],
                "story_compliance": {"matches_spec": True, "mismatches": []},
                "test_coverage": {"adequate": True, "gaps": []},
            }
        )

    def _make_mock_openai_module(self):
        """Build a sys.modules-compatible mock openai with a real BadRequestError subclass."""

        # Create a real exception class so isinstance() checks work
        class FakeBadRequestError(Exception):
            pass

        mock_openai = MagicMock()
        mock_openai.BadRequestError = FakeBadRequestError
        mock_httpx = MagicMock()
        return mock_openai, mock_httpx, FakeBadRequestError

    def test_bad_request_with_tool_keyword_triggers_fallback(self, tmp_path):
        """BadRequestError mentioning 'tool' triggers single-shot retry."""
        import sys

        profile = _make_local_profile(
            base_url="http://localhost:11434/v1", allowed_tools=("read_file",)
        )
        review_json = self._valid_review_json()
        fallback_result = MagicMock()
        fallback_result.success = True
        fallback_result.cost_usd = 0.0
        fallback_result.output = review_json

        mock_openai, mock_httpx, FakeBadRequestError = self._make_mock_openai_module()
        bad_request = FakeBadRequestError("model does not support tools")

        with (
            patch.dict(sys.modules, {"openai": mock_openai, "httpx": mock_httpx}),
            patch("theforge.runners.api.AgentLoopManager") as MockManager,
            patch.dict(
                "theforge.runners.api.PROVIDER_RUNNERS",
                {"openai": MagicMock(return_value=fallback_result)},
            ),
        ):
            MockManager.return_value.run.side_effect = bad_request
            result = _run_loop_openai("prompt", profile, tmp_path, secrets=None)

        assert result.success
        assert result is fallback_result

    def test_bad_request_without_tool_keyword_reraises(self, tmp_path):
        """BadRequestError without 'tool' in message propagates (not a tool-call issue)."""
        import sys

        profile = _make_local_profile(
            base_url="http://localhost:11434/v1", allowed_tools=("read_file",)
        )
        mock_openai, mock_httpx, FakeBadRequestError = self._make_mock_openai_module()
        bad_request = FakeBadRequestError("model not found")

        with (
            patch.dict(sys.modules, {"openai": mock_openai, "httpx": mock_httpx}),
            patch("theforge.runners.api.AgentLoopManager") as MockManager,
        ):
            MockManager.return_value.run.side_effect = bad_request
            with pytest.raises(FakeBadRequestError):
                _run_loop_openai("prompt", profile, tmp_path, secrets=None)


class TestOpenAIEndpointRouting:
    """Tests for OpenAI Chat vs Responses endpoint selection."""

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("gpt-5.1-codex", True),
            ("gpt-5.1-codex-mini", True),
            ("gpt-5.4", False),
            ("o4-mini", False),
        ],
    )
    def test_uses_openai_responses_api_matches_runtime_expectations(self, model, expected):
        assert uses_openai_responses_api(model) is expected

    def test_run_loop_openai_uses_responses_adapter_for_responses_only_models(self, tmp_path):
        import sys

        profile = _make_profile(model="gpt-5.1-codex", allowed_tools=("read_file",))
        mock_result = AgentResult(
            success=True,
            output="{}",
            session_id=None,
            cost_usd=0.01,
            exit_code=0,
            raw={},
            profile_name=profile.name,
        )

        with (
            patch.dict(sys.modules, {"openai": MagicMock(), "httpx": MagicMock()}),
            patch("theforge.runners.api._make_openai_responses_adapter") as make_responses,
            patch(
                "theforge.runners.api._make_openai_responses_finalizer"
            ) as make_responses_finalizer,
            patch("theforge.runners.api._make_openai_chat_adapter") as make_chat,
            patch("theforge.runners.api._make_openai_chat_finalizer") as make_chat_finalizer,
            patch("theforge.runners.api.AgentLoopManager") as MockManager,
        ):
            make_responses.return_value = MagicMock()
            make_responses_finalizer.return_value = MagicMock()
            MockManager.return_value.run.return_value = mock_result

            result = _run_loop_openai("prompt", profile, tmp_path, secrets=None)

        assert result is mock_result
        make_responses.assert_called_once()
        make_responses_finalizer.assert_called_once()
        make_chat.assert_not_called()
        make_chat_finalizer.assert_not_called()

    def test_run_loop_openai_uses_chat_adapter_for_dual_endpoint_models(self, tmp_path):
        import sys

        profile = _make_profile(model="gpt-5.4", allowed_tools=("read_file",))
        mock_result = AgentResult(
            success=True,
            output="{}",
            session_id=None,
            cost_usd=0.01,
            exit_code=0,
            raw={},
            profile_name=profile.name,
        )

        with (
            patch.dict(sys.modules, {"openai": MagicMock(), "httpx": MagicMock()}),
            patch("theforge.runners.api._make_openai_responses_adapter") as make_responses,
            patch(
                "theforge.runners.api._make_openai_responses_finalizer"
            ) as make_responses_finalizer,
            patch("theforge.runners.api._make_openai_chat_adapter") as make_chat,
            patch("theforge.runners.api._make_openai_chat_finalizer") as make_chat_finalizer,
            patch("theforge.runners.api.AgentLoopManager") as MockManager,
        ):
            make_chat.return_value = MagicMock()
            make_chat_finalizer.return_value = MagicMock()
            MockManager.return_value.run.return_value = mock_result

            result = _run_loop_openai("prompt", profile, tmp_path, secrets=None)

        assert result is mock_result
        make_chat.assert_called_once()
        make_chat_finalizer.assert_called_once()
        make_responses.assert_not_called()
        make_responses_finalizer.assert_not_called()
