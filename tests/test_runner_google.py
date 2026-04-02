"""Tests for the Google Gemini provider adapter (_run_google, _make_google_adapter)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from theforge.config import ModelProfile
from theforge.runners.adapters.google import _make_google_adapter, _run_google
from theforge.runners.schema_utils import ToolCallRequest


def _make_profile(
    name: str = "test-reviewer",
    provider: str = "google",
    model: str = "gemini-2.5-flash",
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        cli=None,
        model=model,
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
        max_tool_output_bytes=51200,
    )


def _make_google_modules(fake_client):
    """Build sys.modules mocks for google.genai, wiring attributes correctly."""
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
    return sys.modules, modules


def _make_response(
    text=None,
    block_reason=None,
    finish_reason="STOP",
    input_tokens=100,
    output_tokens=50,
):
    """Build a minimal fake Gemini response object."""
    usage = MagicMock()
    usage.prompt_token_count = input_tokens
    usage.candidates_token_count = output_tokens

    candidate = MagicMock()
    candidate.finish_reason = finish_reason

    feedback = MagicMock()
    feedback.block_reason = block_reason

    response = MagicMock()
    response.text = text
    response.usage_metadata = usage
    response.candidates = [candidate]
    response.prompt_feedback = feedback
    return response


class TestRunGoogle:
    """Tests for the _run_google single-shot API path."""

    def test_valid_json_response_succeeds(self):
        """Normal JSON response returns success with structured_data parsed."""
        valid_json = (
            '{"verdict": "APPROVE", "summary": "ok", "findings": [],'
            ' "story_compliance": {"matches_spec": true, "mismatches": []},'
            ' "test_coverage": {"adequate": true, "gaps": []}}'
        )
        fake_response = _make_response(text=valid_json)
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            result = _run_google("test prompt", profile)

        assert result.success
        assert result.exit_code == 0
        assert result.output == valid_json
        assert result.structured_data is not None

    def test_none_response_text_returns_clear_error(self):
        """response.text is None → clear error, not TypeError."""
        # Both calls return None (initial + retry)
        fake_response = _make_response(text=None, finish_reason="STOP", input_tokens=1234)
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            result = _run_google("test prompt", profile)

        assert not result.success
        assert result.exit_code == 1
        assert "TypeError" not in result.output
        assert "empty" in result.output.lower() or "retry" in result.output.lower()

    def test_blocked_response_surfaces_block_reason_and_tokens(self):
        """Block reason and both token counts are included in the error message."""
        fake_response = _make_response(
            text=None, block_reason="SAFETY", input_tokens=500, output_tokens=10
        )
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            result = _run_google("test prompt", profile)

        assert not result.success
        assert "SAFETY" in result.output
        assert "block_reason" in result.output
        assert "500" in result.output  # input token count
        assert "output_tokens" in result.output

    def test_non_stop_finish_reason_in_error(self):
        """Non-STOP finish_reason (SAFETY, RECITATION, MAX_TOKENS) is logged explicitly."""
        for finish_reason in ("SAFETY", "RECITATION", "MAX_TOKENS"):
            fake_response = _make_response(
                text=None, block_reason=None, finish_reason=finish_reason, input_tokens=200
            )
            fake_client = MagicMock()
            fake_client.models.generate_content.return_value = fake_response

            _, modules = _make_google_modules(fake_client)
            profile = _make_profile()

            with patch.dict(sys.modules, modules):
                result = _run_google("test prompt", profile)

            assert not result.success, f"Expected failure for finish_reason={finish_reason}"
            assert finish_reason in result.output, f"Expected {finish_reason} in output"
            assert "200" in result.output  # input token count
            assert "output_tokens" in result.output

    def test_empty_unblocked_response_retries_once(self):
        """Empty unblocked response (STOP) triggers one retry before giving up."""
        empty_response = _make_response(text=None, finish_reason="STOP")
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = empty_response

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            result = _run_google("test prompt", profile)

        # Should have called generate_content twice (initial + retry)
        assert fake_client.models.generate_content.call_count == 2
        assert not result.success

    def test_retry_accumulates_token_counts(self):
        """Successful retry reports combined token usage from both API calls."""
        valid_json = (
            '{"verdict": "APPROVE", "summary": "ok", "findings": [],'
            ' "story_compliance": {"matches_spec": true, "mismatches": []},'
            ' "test_coverage": {"adequate": true, "gaps": []}}'
        )
        # First call: 300 input, 0 output (empty response)
        empty_response = _make_response(
            text=None, finish_reason="STOP", input_tokens=300, output_tokens=0
        )
        # Second call: 310 input, 80 output (includes cached context)
        good_response = _make_response(text=valid_json, input_tokens=310, output_tokens=80)
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [empty_response, good_response]

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            result = _run_google("test prompt", profile)

        assert fake_client.models.generate_content.call_count == 2
        assert result.success
        assert result.model_usage
        usage = result.model_usage[0]
        # Tokens from both calls must be summed
        assert usage.input_tokens == 300 + 310
        assert usage.output_tokens == 0 + 80

    def test_retry_fails_includes_accumulated_tokens_in_error(self):
        """When retry also fails, error message reflects combined token counts."""
        empty1 = _make_response(text=None, finish_reason="STOP", input_tokens=400, output_tokens=5)
        empty2 = _make_response(text=None, finish_reason="STOP", input_tokens=410, output_tokens=0)
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [empty1, empty2]

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            result = _run_google("test prompt", profile)

        assert not result.success
        # Combined input = 400 + 410 = 810
        assert "810" in result.output


class TestGoogleAdapter:
    """Tests for _make_google_adapter (tool-use loop path)."""

    def test_thought_signature_attached_to_fc(self):
        """Function calls are tagged with thought_signature from preceding thought part."""
        fake_thought_part = MagicMock()
        fake_thought_part.thought = True
        fake_thought_part.thought_signature = "sig-abc"
        fake_thought_part.function_call = None
        fake_thought_part.text = None

        fake_fc = MagicMock()
        fake_fc.name = "submit_review"
        fake_fc.args = {"verdict": "APPROVE"}
        fake_fc.thought_signature = None  # not on the fc itself

        fake_fc_part = MagicMock()
        fake_fc_part.thought = False
        fake_fc_part.function_call = fake_fc
        fake_fc_part.text = None

        fake_candidate = MagicMock()
        fake_candidate.content = MagicMock()
        fake_candidate.content.parts = [fake_thought_part, fake_fc_part]
        fake_candidate.finish_reason = "STOP"

        fake_response = MagicMock()
        fake_response.candidates = [fake_candidate]
        fake_response.usage_metadata = None
        fake_response.prompt_feedback = None

        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile(model="gemini-2.5-pro-preview-customtools")

        with patch.dict(sys.modules, modules):
            adapter = _make_google_adapter(profile, secrets=None)
            turn = adapter([{"role": "user", "content": "review please"}], [])

        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].thought_signature == "sig-abc"

    def test_fc_args_none_parsed_as_empty_dict(self):
        """Google adapter with fc.args = None should produce arguments={}."""
        fake_fc = MagicMock()
        fake_fc.name = "submit_review"
        fake_fc.args = None
        fake_fc.thought_signature = None

        fake_part = MagicMock()
        fake_part.thought = False
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

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            adapter = _make_google_adapter(profile, secrets=None)
            turn = adapter([{"role": "user", "content": "hi"}], [])

        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].arguments == {}

    def test_content_parts_none_yields_empty(self):
        """Google adapter with candidate.content.parts = None → no tool_calls, no text."""
        fake_candidate = MagicMock()
        fake_candidate.content = MagicMock()
        fake_candidate.content.parts = None  # parts is None, not []
        fake_candidate.finish_reason = "STOP"

        fake_response = MagicMock()
        fake_response.candidates = [fake_candidate]
        fake_response.usage_metadata = None
        fake_response.prompt_feedback = None

        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response

        _, modules = _make_google_modules(fake_client)
        profile = _make_profile()

        with patch.dict(sys.modules, modules):
            adapter = _make_google_adapter(profile, secrets=None)
            turn = adapter([{"role": "user", "content": "hi"}], [])

        assert turn.tool_calls == []
        assert turn.text_output is None


class TestTranslateMessagesGoogle:
    """Tests for _translate_messages_google message format conversion."""

    def test_includes_thought_signature(self):
        """_translate_messages_google embeds thought_signature in function_call parts."""
        from theforge.runners.adapters.google import _translate_messages_google

        tc = ToolCallRequest(
            id="call_0",
            name="submit_review",
            arguments={"verdict": "APPROVE"},
            thought_signature="sig-xyz",
        )
        messages = [{"role": "assistant", "tool_calls": [tc], "content": None}]
        result = _translate_messages_google(messages)

        assert len(result) == 1
        fc_part = result[0]["parts"][0]["function_call"]
        assert fc_part["thought_signature"] == "sig-xyz"

    def test_omits_thought_signature_when_absent(self):
        """_translate_messages_google omits thought_signature when None."""
        from theforge.runners.adapters.google import _translate_messages_google

        tc = ToolCallRequest(
            id="call_0",
            name="submit_review",
            arguments={"verdict": "APPROVE"},
            thought_signature=None,
        )
        messages = [{"role": "assistant", "tool_calls": [tc], "content": None}]
        result = _translate_messages_google(messages)

        fc_part = result[0]["parts"][0]["function_call"]
        assert "thought_signature" not in fc_part
