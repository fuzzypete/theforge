"""Forced-output fallbacks must use the contract of the phase in flight.

A plan reviewer that stops emitting tool calls is routed through the provider's
forced-structured-output fallback. When that fallback hardcoded the code-review
contract, the reviewer returned a complete review whose findings carried the
code-review keys, the plan-review parser rejected it, and the reviewer was
dropped from the merged verdict entirely.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

from theforge.config import ModelProfile
from theforge.review import parse_plan_review_output
from theforge.runners.schema_utils import (
    SUBMIT_PLAN_REVIEW,
    SUBMIT_REVIEW,
    _sanitize_schema_for_google,
    forced_output_contract,
)
from theforge.schemas import plan_review_json_schema, review_json_schema

PLAN_REVIEW_JSON = json.dumps(
    {
        "verdict": "APPROVE",
        "summary": "Plan covers the acceptance criteria.",
        "findings": [{"severity": "P2", "description": "Bump the audit record schema version."}],
        "criteria_coverage": [{"criterion": "AC1", "covered": True, "plan_section": "Step 2"}],
    }
)


# What the reviewer actually returned when the code-review contract was forced on
# it: a complete, substantively correct review carrying the code-review keys.
CODE_REVIEW_SHAPED_JSON = json.dumps(
    {
        "verdict": "APPROVE",
        "summary": "Plan covers the acceptance criteria.",
        "findings": [
            {
                "severity": "P2",
                "file": "src/theforge/audit.py",
                "line": 1,
                "observed": "schema version unchanged",
                "expected": "schema version bumped",
                "evidence": "audit record shape changes",
                "suggestion": "Bump the audit record schema version.",
            }
        ],
        "story_compliance": {"matches_spec": True, "mismatches": []},
        "test_coverage": {"adequate": True, "gaps": []},
    }
)


def _make_profile(
    phase: str | None,
    provider: str = "google",
    model: str = "gemini-2.5-flash",
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
    )


def _google_modules(fake_client):
    mock_genai_types = MagicMock()
    mock_genai = MagicMock()
    mock_genai.Client.return_value = fake_client
    mock_genai.types = mock_genai_types
    mock_google = MagicMock()
    mock_google.genai = mock_genai
    return {
        "google": mock_google,
        "google.genai": mock_genai,
        "google.genai.types": mock_genai_types,
    }


def _prose_response():
    """A Gemini turn that returned prose instead of a submit tool call."""
    part = MagicMock()
    part.text = "I reviewed the plan and it looks reasonable."
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


def _json_response(text: str):
    response = MagicMock()
    response.text = text
    response.usage_metadata = None
    response.candidates = []
    response.prompt_feedback = None
    return response


class TestForcedOutputContract:
    """The selector maps a phase to its schema, submit tool, and instruction."""

    def test_plan_review_phase_selects_plan_review_contract(self):
        contract = forced_output_contract("plan_review")
        assert contract.json_schema == plan_review_json_schema()
        assert contract.schema_name == "plan_review_output"
        assert contract.submit_tool == SUBMIT_PLAN_REVIEW
        instruction = contract.instruction()
        assert "criteria_coverage" in instruction
        assert "story_compliance" not in instruction
        assert "test_coverage" not in instruction

    def test_review_phase_selects_code_review_contract(self):
        contract = forced_output_contract("review")
        assert contract.json_schema == review_json_schema()
        assert contract.schema_name == "review_output"
        assert contract.submit_tool == SUBMIT_REVIEW
        instruction = contract.instruction()
        assert "story_compliance" in instruction
        assert "test_coverage" in instruction

    def test_unknown_phase_defaults_to_code_review_contract(self):
        assert forced_output_contract(None).json_schema == review_json_schema()

    def test_instruction_form_and_suffix_are_composable(self):
        contract = forced_output_contract("plan_review")
        assert contract.instruction(prefix="") == (
            "Deliver your plan review verdict now as structured JSON. "
            "Include verdict, summary, findings, and criteria_coverage."
        )
        assert contract.instruction(form=None).startswith(
            "Time is up. Deliver your plan review verdict now. Include"
        )
        assert contract.instruction(form="JSON", suffix=" No fences.").endswith(" No fences.")


class TestGoogleAdapterFallbackContract:
    """_make_google_adapter's mid-loop fallback follows profile.phase."""

    def _run_adapter(self, phase: str, final_json: str):
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [
            _prose_response(),
            _json_response(final_json),
        ]
        modules = _google_modules(fake_client)
        with patch.dict(sys.modules, modules):
            from theforge.runners.adapters.google import _make_google_adapter

            adapter = _make_google_adapter(_make_profile(phase), secrets=None)
            turn = adapter([{"role": "user", "content": "review this plan"}], [])
        return turn, fake_client, modules

    def test_plan_review_fallback_uses_plan_review_schema(self):
        _, fake_client, modules = self._run_adapter("plan_review", PLAN_REVIEW_JSON)

        assert fake_client.models.generate_content.call_count == 2
        config_kwargs = modules["google.genai.types"].GenerateContentConfig.call_args.kwargs
        assert config_kwargs["response_schema"] == _sanitize_schema_for_google(
            plan_review_json_schema()
        )

    def test_plan_review_fallback_instruction_names_plan_fields(self):
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = [
            _prose_response(),
            _json_response(PLAN_REVIEW_JSON),
        ]
        with patch.dict(sys.modules, _google_modules(fake_client)):
            from theforge.runners.adapters.google import _make_google_adapter

            adapter = _make_google_adapter(_make_profile("plan_review"), secrets=None)
            adapter([{"role": "user", "content": "review this plan"}], [])

        finalize_contents = fake_client.models.generate_content.call_args.kwargs["contents"]
        instruction = finalize_contents[-1]["parts"][0]["text"]
        assert "criteria_coverage" in instruction
        assert "story_compliance" not in instruction

    def test_plan_review_fallback_output_parses_as_plan_review(self):
        """Seam: the fallback's output is accepted by the plan-review parser.

        This is the fix-success criterion — no parse error, verdict preserved,
        and the reviewer's finding survives instead of being dropped. The fake
        model answers in whatever shape the forced schema asks for, so forcing
        the code-review contract reproduces the original unparseable output.
        """
        fake_client = MagicMock()
        modules = _google_modules(fake_client)
        plan_schema = _sanitize_schema_for_google(plan_review_json_schema())
        calls = {"n": 0}

        def _respond(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _prose_response()
            config_kwargs = modules["google.genai.types"].GenerateContentConfig.call_args.kwargs
            if config_kwargs["response_schema"] == plan_schema:
                return _json_response(PLAN_REVIEW_JSON)
            return _json_response(CODE_REVIEW_SHAPED_JSON)

        fake_client.models.generate_content.side_effect = _respond
        with patch.dict(sys.modules, modules):
            from theforge.runners.adapters.google import _make_google_adapter

            adapter = _make_google_adapter(_make_profile("plan_review"), secrets=None)
            turn = adapter([{"role": "user", "content": "review this plan"}], [])

        assert turn.structured_data is not None
        result = parse_plan_review_output(turn.text_output or "")
        assert result.parse_errors == []
        assert result.verdict == "APPROVE"
        assert len(result.findings) == 1
        assert result.findings[0].description == "Bump the audit record schema version."

    def test_code_review_fallback_still_uses_code_review_schema(self):
        code_review_json = json.dumps(
            {
                "verdict": "APPROVE",
                "summary": "ok",
                "findings": [],
                "story_compliance": {"matches_spec": True, "mismatches": []},
                "test_coverage": {"adequate": True, "gaps": []},
            }
        )
        _, fake_client, modules = self._run_adapter("review", code_review_json)

        assert fake_client.models.generate_content.call_count == 2
        config_kwargs = modules["google.genai.types"].GenerateContentConfig.call_args.kwargs
        assert config_kwargs["response_schema"] == _sanitize_schema_for_google(
            review_json_schema()
        )


class TestGoogleFinalizerContract:
    """_make_google_finalizer (timeout path) follows profile.phase too."""

    def _run_finalizer(self, phase: str, final_json: str):
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = _json_response(final_json)
        modules = _google_modules(fake_client)
        with patch.dict(sys.modules, modules):
            from theforge.runners.finalizers import _make_google_finalizer

            finalizer = _make_google_finalizer(_make_profile(phase), secrets=None)
            turn = finalizer([{"role": "user", "content": "review this plan"}])
        return turn, fake_client, modules

    def test_plan_review_timeout_uses_plan_review_schema(self):
        turn, fake_client, modules = self._run_finalizer("plan_review", PLAN_REVIEW_JSON)

        config_kwargs = modules["google.genai.types"].GenerateContentConfig.call_args.kwargs
        assert config_kwargs["response_schema"] == _sanitize_schema_for_google(
            plan_review_json_schema()
        )
        contents = fake_client.models.generate_content.call_args.kwargs["contents"]
        assert "criteria_coverage" in contents[-1]["parts"][0]["text"]
        assert parse_plan_review_output(turn.text_output or "").parse_errors == []

    def test_code_review_timeout_uses_code_review_schema(self):
        _, _, modules = self._run_finalizer("review", "{}")

        config_kwargs = modules["google.genai.types"].GenerateContentConfig.call_args.kwargs
        assert config_kwargs["response_schema"] == _sanitize_schema_for_google(
            review_json_schema()
        )


class TestAnthropicFinalizerContract:
    """The Anthropic finalizer forces the submit tool of the phase in flight."""

    def _run_finalizer(self, phase: str, tool_name: str, tool_input: dict):
        mock_anthropic = MagicMock()
        fake_client = MagicMock()
        mock_anthropic.Anthropic.return_value = fake_client

        block = MagicMock()
        block.type = "tool_use"
        block.name = tool_name
        block.input = tool_input
        response = MagicMock()
        response.content = [block]
        response.usage.input_tokens = 10
        response.usage.output_tokens = 5
        fake_client.messages.create.return_value = response

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            from theforge.runners.finalizers import _make_anthropic_finalizer

            finalizer = _make_anthropic_finalizer(
                _make_profile(phase, provider="anthropic", model="claude-sonnet-4-6"),
                secrets=None,
            )
            turn = finalizer([{"role": "user", "content": "review this plan"}])
        return turn, fake_client

    def test_plan_review_forces_submit_plan_review(self):
        plan_data = json.loads(PLAN_REVIEW_JSON)
        turn, fake_client = self._run_finalizer("plan_review", SUBMIT_PLAN_REVIEW, plan_data)

        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": SUBMIT_PLAN_REVIEW}
        assert [t["name"] for t in kwargs["tools"]] == [SUBMIT_PLAN_REVIEW]
        assert "criteria_coverage" in kwargs["messages"][-1]["content"]
        assert turn.structured_data == plan_data
        assert parse_plan_review_output(turn.text_output or "").parse_errors == []

    def test_code_review_forces_submit_review(self):
        _, fake_client = self._run_finalizer("review", SUBMIT_REVIEW, {"verdict": "APPROVE"})

        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": SUBMIT_REVIEW}
        assert [t["name"] for t in kwargs["tools"]] == [SUBMIT_REVIEW]


class TestOpenAIResponsesFinalizerContract:
    """The Responses-API finalizer follows profile.phase."""

    def _run_finalizer(self, phase: str, output_text: str):
        mock_openai = MagicMock()
        mock_openai.BadRequestError = Exception
        mock_httpx = MagicMock()
        fake_client = MagicMock()
        response = MagicMock()
        response.output_text = output_text
        response.usage = None
        fake_client.responses.create.return_value = response

        with patch.dict(sys.modules, {"openai": mock_openai, "httpx": mock_httpx}):
            from theforge.runners import finalizers

            with patch(
                "theforge.runners.adapters.openai._openai_client", return_value=fake_client
            ):
                finalizer = finalizers._make_openai_responses_finalizer(
                    _make_profile(phase, provider="openai", model="gpt-5.1-codex"),
                    secrets=None,
                )
                turn = finalizer([{"role": "user", "content": "review this plan"}])
        return turn, fake_client

    def test_plan_review_uses_plan_review_schema(self):
        turn, fake_client = self._run_finalizer("plan_review", PLAN_REVIEW_JSON)

        kwargs = fake_client.responses.create.call_args.kwargs
        fmt = kwargs["text"]["format"]
        assert fmt["name"] == "plan_review_output"
        assert fmt["schema"] == plan_review_json_schema()
        assert "criteria_coverage" in kwargs["input"][-1]["content"]
        assert parse_plan_review_output(turn.text_output or "").parse_errors == []

    def test_code_review_uses_code_review_schema(self):
        _, fake_client = self._run_finalizer("review", "{}")

        fmt = fake_client.responses.create.call_args.kwargs["text"]["format"]
        assert fmt["name"] == "review_output"
        assert fmt["schema"] == review_json_schema()
