"""Request construction must be safe for models the code predates.

Optional sampling controls (``temperature``) used to be decided at each request
construction site, either unguarded or from the shape of the model's *name*. A
name is not a capability, so any model released after the guard was written was
misclassified and every request to it failed with HTTP 400. These tests pin the
single shared decision — :func:`sampling_control_kwargs` — and assert that no
request builder in the runners subsystem reintroduces the parameter, including
for model names that did not exist when this test was written.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from theforge.config import ModelProfile
from theforge.runners.schema_utils import (
    openai_function_tool_request_shape,
    sampling_control_kwargs,
)

# Names deliberately chosen to span "known at authoring time" and "not yet
# released": the policy must be identical for all of them.
FUTURE_AND_CURRENT_MODELS = ["gpt-4o", "o3-mini", "gpt-5.6-sol", "gpt-5.6-terra"]

REVIEW_JSON = json.dumps(
    {
        "verdict": "APPROVE",
        "summary": "ok",
        "findings": [],
        "story_compliance": {"matches_spec": True, "mismatches": []},
        "test_coverage": {"adequate": True, "gaps": []},
    }
)


def _profile(
    provider: str = "openai",
    model: str = "gpt-5.6-sol",
    phase: str | None = "review",
    thinking_budget: int | None = None,
) -> ModelProfile:
    return ModelProfile(
        name="test-reviewer",
        provider=provider,
        cli=None,
        model=model,
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
        phase=phase,
        thinking_budget=thinking_budget,
    )


def _openai_chat_client() -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.choices[0].message.content = REVIEW_JSON
    response.choices[0].message.tool_calls = None
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    response.model_dump.return_value = {}
    client.chat.completions.create.return_value = response
    return client


class TestSamplingControlPolicy:
    """The shared decision point sends no optional sampling controls."""

    def test_no_sampling_controls_are_sent(self):
        assert sampling_control_kwargs() == {}

    def test_decision_takes_no_model_input(self):
        """A policy that cannot see a model name cannot go stale against one."""
        import inspect

        assert inspect.signature(sampling_control_kwargs).parameters == {}


class TestOpenAIFunctionToolRequestShape:
    def test_responses_only_models_route_tools_to_responses(self):
        shape = openai_function_tool_request_shape("gpt-5.1-codex")

        assert shape.transport == "responses"
        assert shape.chat_extra_kwargs() == {}

    @pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra"])
    def test_observed_reasoning_models_send_reasoning_effort_none_for_tools(self, model):
        shape = openai_function_tool_request_shape(model)

        assert shape.transport == "chat"
        assert shape.chat_extra_kwargs() == {"reasoning_effort": "none"}

    def test_tool_capability_defaults_to_plain_chat_when_no_override_is_needed(self):
        shape = openai_function_tool_request_shape("gpt-4o")

        assert shape.transport == "chat"
        assert shape.chat_extra_kwargs() == {}

    def test_unprobed_gpt5_chat_models_keep_default_reasoning_for_tools(self):
        shape = openai_function_tool_request_shape("gpt-5.4")

        assert shape.transport == "chat"
        assert shape.chat_extra_kwargs() == {}


class TestOpenAICompatibleRequests:
    """Chat Completions single-shot, loop adapter, and finalizers."""

    @pytest.mark.parametrize("model", FUTURE_AND_CURRENT_MODELS)
    def test_single_shot_omits_temperature(self, model):
        from theforge.runners.adapters.openai import _run_openai_chat

        client = _openai_chat_client()
        result = _run_openai_chat("review this", _profile(model=model), client=client)

        assert result.success
        kwargs = client.chat.completions.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["response_format"]["type"] == "json_schema"

    @pytest.mark.parametrize("model", FUTURE_AND_CURRENT_MODELS)
    def test_loop_adapter_omits_temperature_and_keeps_tools(self, model):
        from theforge.runners.adapters.openai import _make_openai_chat_adapter

        client = _openai_chat_client()
        adapter = _make_openai_chat_adapter(_profile(model=model), None, client=client)
        adapter([{"role": "user", "content": "go"}], [{"type": "function", "name": "read_file"}])

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["tools"] == [{"type": "function", "name": "read_file"}]

    def test_loop_adapter_accepts_provider_specific_tool_controls(self):
        from theforge.runners.adapters.openai import _make_openai_chat_adapter

        client = _openai_chat_client()
        adapter = _make_openai_chat_adapter(
            _profile(model="gpt-5.6-sol"),
            None,
            client=client,
            extra_kwargs={"reasoning_effort": "none"},
        )
        adapter([{"role": "user", "content": "go"}], [{"type": "function", "name": "read_file"}])

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["reasoning_effort"] == "none"
        assert kwargs["tools"] == [{"type": "function", "name": "read_file"}]

    @pytest.mark.parametrize("model", FUTURE_AND_CURRENT_MODELS)
    def test_chat_finalizer_omits_temperature_and_keeps_schema(self, model):
        from theforge.runners.finalizers import _make_openai_chat_finalizer

        client = _openai_chat_client()
        finalizer = _make_openai_chat_finalizer(_profile(model=model), None, client=client)
        finalizer([{"role": "user", "content": "go"}])

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["response_format"]["json_schema"]["name"] == "review_output"

    @pytest.mark.parametrize("model", ["deepseek-v3", "deepseek-r1", "deepseek-next"])
    def test_deepseek_finalizer_omits_temperature_and_keeps_json_mode(self, model):
        from theforge.runners.finalizers import _make_deepseek_finalizer

        client = _openai_chat_client()
        finalizer = _make_deepseek_finalizer(
            _profile(provider="deepseek", model=model), None, client=client
        )
        finalizer([{"role": "user", "content": "go"}])

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["response_format"] == {"type": "json_object"}

    @pytest.mark.parametrize("model", ["deepseek-v3", "deepseek-next"])
    def test_deepseek_loop_adapter_omits_temperature(self, model):
        from theforge.runners.adapters.deepseek import _make_deepseek_adapter

        client = _openai_chat_client()
        with patch("theforge.runners.adapters.deepseek._deepseek_client", return_value=client):
            adapter = _make_deepseek_adapter(_profile(provider="deepseek", model=model), None)
        adapter([{"role": "user", "content": "go"}], [])

        assert "temperature" not in client.chat.completions.create.call_args.kwargs


def _anthropic_modules(client: MagicMock) -> dict:
    mock_anthropic = MagicMock()
    mock_anthropic.Anthropic.return_value = client
    return {"anthropic": mock_anthropic}


def _anthropic_tool_response(tool_name: str) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = json.loads(REVIEW_JSON)
    response = MagicMock()
    response.content = [block]
    response.usage.input_tokens = 10
    response.usage.output_tokens = 5
    response.model_dump.return_value = {}
    return response


class TestAnthropicRequests:
    """Single-shot, agent-loop turns, and the forced-output finalizer."""

    @pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-future-9"])
    def test_single_shot_omits_temperature_and_keeps_tool_choice(self, model):
        client = MagicMock()
        client.messages.create.return_value = _anthropic_tool_response("review_output")

        with patch.dict(sys.modules, _anthropic_modules(client)):
            from theforge.runners.adapters.anthropic import _run_anthropic

            result = _run_anthropic("review this", _profile(provider="anthropic", model=model))

        assert result.success, result.output
        kwargs = client.messages.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["max_tokens"] == 4096
        assert kwargs["tool_choice"] == {"type": "tool", "name": "review_output"}

    @pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-future-9"])
    def test_single_shot_plain_text_omits_forced_review_tool(self, model):
        client = MagicMock()
        response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "run_summary:\n  summary: kept as text"
        response.content = [text_block]
        response.usage.input_tokens = 10
        response.usage.output_tokens = 5
        response.model_dump.return_value = {}
        client.messages.create.return_value = response

        with patch.dict(sys.modules, _anthropic_modules(client)):
            from theforge.runners.adapters.anthropic import _run_anthropic

            result = _run_anthropic(
                "summarize this",
                _profile(provider="anthropic", model=model),
                plain_text=True,
            )

        assert result.success, result.output
        assert result.output == "run_summary:\n  summary: kept as text"
        assert result.structured_data is None
        kwargs = client.messages.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["max_tokens"] == 4096
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs

    @pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-future-9"])
    def test_loop_adapter_omits_temperature_and_keeps_tools(self, model):
        client = MagicMock()
        response = MagicMock()
        response.content = []
        response.usage.input_tokens = 10
        response.usage.output_tokens = 5
        client.messages.create.return_value = response
        tools = [{"name": "read_file", "input_schema": {"type": "object"}}]

        with patch.dict(sys.modules, _anthropic_modules(client)):
            from theforge.runners.adapters.anthropic import _make_anthropic_adapter

            adapter = _make_anthropic_adapter(_profile(provider="anthropic", model=model), None)
            adapter([{"role": "user", "content": "go"}], tools)

        kwargs = client.messages.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["max_tokens"] == 8192
        assert kwargs["tools"] == tools
        assert "tool_choice" not in kwargs

    def test_finalizer_omits_temperature_and_keeps_forced_tool(self):
        client = MagicMock()
        client.messages.create.return_value = _anthropic_tool_response("submit_review")

        with patch.dict(sys.modules, _anthropic_modules(client)):
            from theforge.runners.finalizers import _make_anthropic_finalizer

            finalizer = _make_anthropic_finalizer(
                _profile(provider="anthropic", model="claude-future-9"), None
            )
            finalizer([{"role": "user", "content": "go"}])

        kwargs = client.messages.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["max_tokens"] == 8192
        assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_review"}


def _google_modules(client: MagicMock) -> dict:
    genai_types = MagicMock()
    genai = MagicMock()
    genai.Client.return_value = client
    genai.types = genai_types
    google = MagicMock()
    google.genai = genai
    return {"google": google, "google.genai": genai, "google.genai.types": genai_types}


def _google_json_response(text: str) -> MagicMock:
    response = MagicMock()
    response.text = text
    response.usage_metadata = None
    response.candidates = []
    response.prompt_feedback = None
    return response


def _google_prose_response() -> MagicMock:
    """A Gemini turn that returned prose instead of a submit tool call."""
    part = MagicMock()
    part.text = "I reviewed it and it looks fine."
    part.function_call = None
    candidate = MagicMock()
    candidate.finish_reason = "STOP"
    candidate.content = MagicMock()
    candidate.content.parts = [part]
    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = None
    response.prompt_feedback = None
    return response


def _all_config_kwargs(modules: dict) -> list[dict]:
    return [c.kwargs for c in modules["google.genai.types"].GenerateContentConfig.call_args_list]


class TestGoogleRequests:
    """Every GenerateContentConfig this subsystem builds."""

    @pytest.mark.parametrize("plain_text", [True, False])
    def test_single_shot_omits_temperature(self, plain_text):
        client = MagicMock()
        client.models.generate_content.return_value = _google_json_response(REVIEW_JSON)
        modules = _google_modules(client)

        with patch.dict(sys.modules, modules):
            from theforge.runners.adapters.google import _run_google

            result = _run_google(
                "review this",
                _profile(provider="google", model="gemini-future-1"),
                plain_text=plain_text,
            )

        assert result.success, result.output
        configs = _all_config_kwargs(modules)
        assert configs and all("temperature" not in c for c in configs)
        if not plain_text:
            assert configs[-1]["response_mime_type"] == "application/json"
            assert "response_schema" in configs[-1]

    def test_loop_adapter_omits_temperature_and_keeps_thinking_config(self):
        client = MagicMock()
        client.models.generate_content.return_value = _google_json_response(REVIEW_JSON)
        modules = _google_modules(client)

        with patch.dict(sys.modules, modules):
            from theforge.runners.adapters.google import _make_google_adapter

            profile = _profile(
                provider="google", model="gemini-future-1", phase="dev", thinking_budget=1024
            )
            adapter = _make_google_adapter(profile, None)
            adapter([{"role": "user", "content": "go"}], [])

        configs = _all_config_kwargs(modules)
        assert configs and all("temperature" not in c for c in configs)
        assert "thinking_config" in configs[-1]

    def test_mid_loop_finalization_omits_temperature_and_keeps_schema(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            _google_prose_response(),
            _google_json_response(REVIEW_JSON),
        ]
        modules = _google_modules(client)

        with patch.dict(sys.modules, modules):
            from theforge.runners.adapters.google import _make_google_adapter

            adapter = _make_google_adapter(
                _profile(provider="google", model="gemini-future-1"), None
            )
            adapter([{"role": "user", "content": "go"}], [])

        assert client.models.generate_content.call_count == 2
        configs = _all_config_kwargs(modules)
        assert all("temperature" not in c for c in configs)
        assert "response_schema" in configs[-1]

    def test_timeout_finalizer_omits_temperature_and_keeps_schema(self):
        client = MagicMock()
        client.models.generate_content.return_value = _google_json_response(REVIEW_JSON)
        modules = _google_modules(client)

        with patch.dict(sys.modules, modules):
            from theforge.runners.finalizers import _make_google_finalizer

            finalizer = _make_google_finalizer(
                _profile(provider="google", model="gemini-future-1"), None
            )
            finalizer([{"role": "user", "content": "go"}])

        configs = _all_config_kwargs(modules)
        assert configs and all("temperature" not in c for c in configs)
        assert configs[-1]["response_mime_type"] == "application/json"
        assert "response_schema" in configs[-1]

    def test_explicit_config_kwargs_still_win(self):
        """The shared policy merges under caller kwargs, never over them."""
        modules = _google_modules(MagicMock())
        with patch.dict(sys.modules, modules):
            from theforge.runners.adapters.google import _make_google_generate_config

            _make_google_generate_config(
                modules["google.genai.types"],
                _profile(provider="google", model="gemini-future-1"),
                response_mime_type="application/json",
            )

        assert _all_config_kwargs(modules)[-1] == {"response_mime_type": "application/json"}
