"""Tests that submit tools are only injected for review phases, not preflight/dev."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.config import ModelProfile
from theforge.runners.schema_utils import (
    _NO_SUBMIT_PHASES,
    SUBMIT_PLAN_REVIEW,
    SUBMIT_REVIEW,
)


def _make_profile(name: str, provider: str = "openai", model: str = "gpt-4o") -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        cli=None,
        model=model,
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=("read_file", "bash"),
    )


def _tool_names_from_openai_schemas(schemas: list[dict]) -> set[str]:
    names = set()
    for s in schemas:
        if "function" in s:
            names.add(s["function"]["name"])
        elif "name" in s:
            names.add(s["name"])
    return names


def _tool_names_from_google_schemas(schemas: list[dict]) -> set[str]:
    return {s["name"] for s in schemas if "name" in s}


def _tool_names_from_anthropic_schemas(schemas: list[dict]) -> set[str]:
    return {s["name"] for s in schemas if "name" in s}


class TestNoSubmitPhases:
    def test_no_submit_phases_contains_preflight_and_dev(self):
        assert "preflight" in _NO_SUBMIT_PHASES
        assert "dev" in _NO_SUBMIT_PHASES

    def test_reviewer_not_in_no_submit_phases(self):
        assert "openai-reviewer" not in _NO_SUBMIT_PHASES
        assert "gemini-reviewer" not in _NO_SUBMIT_PHASES
        assert "openai-plan-reviewer" not in _NO_SUBMIT_PHASES


class TestOpenAISubmitGating:
    def _captured_tool_schemas(self, profile: ModelProfile, tmp_path: Path) -> list[dict]:
        captured = {}

        def fake_run(initial_messages, tool_schemas):
            captured["schemas"] = tool_schemas
            raise RuntimeError("stop")

        with (
            patch("theforge.runners.api._make_openai_chat_adapter"),
            patch("theforge.runners.api._make_openai_chat_finalizer"),
            patch("theforge.runners.api.AgentLoopManager") as mock_mgr,
        ):
            mock_mgr.return_value.run.side_effect = RuntimeError("stop")
            from theforge.runners.api import _run_loop_openai

            try:
                _run_loop_openai("prompt", profile, tmp_path)
            except RuntimeError:
                pass
            return mock_mgr.return_value.run.call_args[1]["tool_schemas"]

    def test_preflight_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("preflight")
        schemas = self._captured_tool_schemas(profile, tmp_path)
        names = _tool_names_from_openai_schemas(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names

    def test_dev_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("dev")
        schemas = self._captured_tool_schemas(profile, tmp_path)
        names = _tool_names_from_openai_schemas(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names

    def test_reviewer_gets_submit_tools(self, tmp_path):
        profile = _make_profile("openai-reviewer")
        schemas = self._captured_tool_schemas(profile, tmp_path)
        names = _tool_names_from_openai_schemas(schemas)
        assert SUBMIT_REVIEW in names
        assert SUBMIT_PLAN_REVIEW in names


class TestGoogleSubmitGating:
    def _captured_tool_schemas(self, profile: ModelProfile, tmp_path: Path) -> list[dict]:
        with (
            patch("theforge.runners.api._make_google_adapter"),
            patch("theforge.runners.api._make_google_finalizer"),
            patch("theforge.runners.api.AgentLoopManager") as mock_mgr,
        ):
            mock_mgr.return_value.run.side_effect = RuntimeError("stop")
            from theforge.runners.api import _run_loop_google

            try:
                _run_loop_google("prompt", profile, tmp_path)
            except RuntimeError:
                pass
            return mock_mgr.return_value.run.call_args[1]["tool_schemas"]

    def test_preflight_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("preflight", provider="google", model="gemini-2.5-pro")
        schemas = self._captured_tool_schemas(profile, tmp_path)
        names = _tool_names_from_google_schemas(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names

    def test_reviewer_gets_submit_tools(self, tmp_path):
        profile = _make_profile("gemini-reviewer", provider="google", model="gemini-2.5-pro")
        schemas = self._captured_tool_schemas(profile, tmp_path)
        names = _tool_names_from_google_schemas(schemas)
        assert SUBMIT_REVIEW in names
        assert SUBMIT_PLAN_REVIEW in names


class TestDeepSeekSubmitGating:
    def _captured_tool_schemas(self, profile: ModelProfile, tmp_path: Path) -> list[dict]:
        with (
            patch("theforge.runners.adapters.deepseek._deepseek_client"),
            patch("theforge.runners.api._make_openai_chat_adapter"),
            patch("theforge.runners.api._make_deepseek_finalizer"),
            patch("theforge.runners.api.AgentLoopManager") as mock_mgr,
        ):
            mock_mgr.return_value.run.side_effect = RuntimeError("stop")
            from theforge.runners.api import _run_loop_deepseek

            try:
                _run_loop_deepseek("prompt", profile, tmp_path)
            except RuntimeError:
                pass
            return mock_mgr.return_value.run.call_args[1]["tool_schemas"]

    def test_dev_gets_no_submit_tools(self, tmp_path):
        profile = _make_profile("dev", provider="deepseek", model="deepseek-reasoner")
        schemas = self._captured_tool_schemas(profile, tmp_path)
        names = _tool_names_from_openai_schemas(schemas)
        assert SUBMIT_REVIEW not in names
        assert SUBMIT_PLAN_REVIEW not in names

    def test_plan_reviewer_gets_submit_tools(self, tmp_path):
        profile = _make_profile(
            "deepseek-plan-reviewer", provider="deepseek", model="deepseek-reasoner"
        )
        schemas = self._captured_tool_schemas(profile, tmp_path)
        names = _tool_names_from_openai_schemas(schemas)
        assert SUBMIT_REVIEW in names
        assert SUBMIT_PLAN_REVIEW in names
