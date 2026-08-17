"""Tests for API provider finalization, integration, rate-limit retry, DeepSeek, and file tools."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from theforge.agent_types import ModelUsage
from theforge.config import ModelProfile
from theforge.runners.adapters.deepseek import _deepseek_client, _run_deepseek
from theforge.runners.api import (
    AgentLoopManager,
    run_api_agent,
)
from theforge.runners.schema_utils import (
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


class TestFinalization:
    """Tests for forced-output finalization on timeout."""

    def test_finalization_on_wall_clock_timeout(self, tmp_path):
        """When wall-clock times out, finalizer is called and returns structured data."""
        review_data = {
            "verdict": "APPROVE",
            "summary": "Finalized review",
            "findings": [],
            "story_compliance": {"matches_spec": True, "mismatches": []},
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
        with patch("theforge.runners.api.time") as mock_time:
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
            "findings": [
                {
                    "severity": "P1",
                    "file": "x.py",
                    "observed": "bug",
                    "expected": "project-contract category rule (test fixture)",
                    "evidence": "(test fixture evidence)",
                }
            ],
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
        with patch.dict("theforge.runners.api._LOOP_RUNNERS", {"openai": mock_fn}):
            run_api_agent(
                prompt="review",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
            )
            mock_fn.assert_called_once_with("review", profile, tmp_path, None, progress_cb=None)

    def test_google_plain_text_with_tools_bypasses_loop_runner(self, tmp_path):
        profile = _make_profile(
            provider="google",
            model="gemini-2.5-flash",
            allowed_tools=("read_file",),
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = None

        loop_runner = MagicMock()
        single_shot_runner = MagicMock(return_value=mock_result)
        with (
            patch.dict("theforge.runners.api._LOOP_RUNNERS", {"google": loop_runner}),
            patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"google": single_shot_runner}),
        ):
            run_api_agent(
                prompt="ideate",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
                plain_text=True,
            )

        loop_runner.assert_not_called()
        single_shot_runner.assert_called_once_with("ideate", profile, None, plain_text=True)

    def test_anthropic_plain_text_with_tools_stays_on_loop_runner(self, tmp_path):
        profile = _make_profile(
            provider="anthropic",
            model="claude-sonnet-4-6",
            allowed_tools=("read_file",),
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = None

        loop_runner = MagicMock()
        single_shot_runner = MagicMock(return_value=mock_result)
        with (
            patch.dict("theforge.runners.api._LOOP_RUNNERS", {"anthropic": loop_runner}),
            patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"anthropic": single_shot_runner}),
        ):
            run_api_agent(
                prompt="summarize",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
                plain_text=True,
            )

        loop_runner.assert_called_once_with("summarize", profile, tmp_path, None, progress_cb=None)
        single_shot_runner.assert_not_called()

    def test_openai_plain_text_tool_free_uses_single_shot_runner(self, tmp_path):
        profile = _make_profile(
            provider="openai",
            model="gpt-5.4",
            allowed_tools=(),
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = None

        single_shot_runner = MagicMock(return_value=mock_result)
        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"openai": single_shot_runner}):
            run_api_agent(
                prompt="summarize",
                profile=profile,
                working_dir=tmp_path,
                quiet=True,
                plain_text=True,
            )

        single_shot_runner.assert_called_once_with("summarize", profile, None, plain_text=True)

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

        with patch("theforge.runners.api.time") as mock_time:
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

        with pytest.raises(ValueError, match="something else broke"):
            manager._call_with_retry([{"role": "user", "content": "hi"}], [])

        assert call_count[0] == 1  # no retry

    def test_gives_up_when_deadline_too_close(self, tmp_path):
        """If remaining time < backoff wait, raises immediately without sleeping."""

        def adapter(messages, tools):
            raise RuntimeError("429 quota exceeded")

        manager = self._make_manager(tmp_path, timeout_seconds=10)
        manager._adapter = adapter

        # Make deadline appear very close (5s left, first backoff = 30s)
        near_deadline = manager._deadline - 5

        with patch("theforge.runners.api.time") as mock_time:
            mock_time.monotonic.return_value = near_deadline
            mock_time.sleep = MagicMock()
            with pytest.raises(RuntimeError, match="429"):
                manager._call_with_retry([{"role": "user", "content": "hi"}], [])

        mock_time.sleep.assert_not_called()

    def test_exhausts_all_retries_then_raises(self, tmp_path):
        """After _MAX_RATE_LIMIT_RETRIES retries, raises the last exception."""
        from theforge.runners.api import _MAX_RATE_LIMIT_RETRIES

        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            raise RuntimeError("429 rate limit every time")

        manager = self._make_manager(tmp_path, timeout_seconds=9999)
        manager._adapter = adapter

        with patch("theforge.runners.api.time") as mock_time:
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
                "story_compliance": {"matches_spec": True, "mismatches": []},
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

        with patch(
            "theforge.runners.adapters.deepseek._deepseek_client", return_value=mock_client
        ):
            result = _run_deepseek("review this", profile)

        assert result.success
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "deepseek-v3"
        # Optional sampling controls are never sent — see sampling_control_kwargs
        assert "temperature" not in call_kwargs

    def test_run_deepseek_r1_skips_temperature(self, tmp_path):
        profile = self._make_deepseek_profile(model="deepseek-r1")
        mock_client = MagicMock()
        self._mock_review_response(mock_client)

        with patch(
            "theforge.runners.adapters.deepseek._deepseek_client", return_value=mock_client
        ):
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
        with patch.dict("theforge.runners.api.PROVIDER_RUNNERS", {"deepseek": mock_fn}):
            run_api_agent(prompt="review", profile=profile, working_dir=tmp_path, quiet=True)
        mock_fn.assert_called_once()

    def test_run_api_agent_deepseek_loop_dispatches(self, tmp_path):
        profile = self._make_deepseek_profile(allowed_tools=("read_file",))
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.cost_usd = None
        mock_fn = MagicMock(return_value=mock_result)
        with patch.dict("theforge.runners.api._LOOP_RUNNERS", {"deepseek": mock_fn}):
            run_api_agent(prompt="review", profile=profile, working_dir=tmp_path, quiet=True)
        mock_fn.assert_called_once_with("review", profile, tmp_path, None, progress_cb=None)


class TestWriteFileHandler:
    """Unit tests for the write_file tool handler."""

    def test_write_creates_file(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_write_file

        result = _handle_write_file(path="hello.py", content="x = 1\n", working_dir=tmp_path)
        assert "hello.py" in result
        assert (tmp_path / "hello.py").read_text() == "x = 1\n"

    def test_write_overwrites_existing_file(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_write_file

        (tmp_path / "a.py").write_text("old content", encoding="utf-8")
        _handle_write_file(path="a.py", content="new content", working_dir=tmp_path)
        assert (tmp_path / "a.py").read_text() == "new content"

    def test_write_creates_parent_directories(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_write_file

        result = _handle_write_file(
            path="nested/deep/file.py", content="# hi\n", working_dir=tmp_path
        )
        assert (tmp_path / "nested" / "deep" / "file.py").exists()
        assert "nested/deep/file.py" in result

    def test_write_rejects_path_traversal(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_write_file

        outside = tmp_path.parent / "outside.py"
        if outside.exists():
            outside.unlink()

        result = _handle_write_file(path="../outside.py", content="bad", working_dir=tmp_path)
        assert result.startswith("Error: path traversal rejected")
        assert not outside.exists()


class TestEditFileHandler:
    """Unit tests for the edit_file tool handler."""

    def test_edit_replaces_unique_string(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_edit_file

        (tmp_path / "f.py").write_text("def foo(): pass\n", encoding="utf-8")
        result = _handle_edit_file(
            path="f.py",
            old_string="def foo(): pass",
            new_string="def foo(): return 1",
            working_dir=tmp_path,
        )
        assert "Replaced 1 occurrence" in result
        assert (tmp_path / "f.py").read_text() == "def foo(): return 1\n"

    def test_edit_errors_on_zero_matches(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_edit_file

        (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
        result = _handle_edit_file(
            path="f.py",
            old_string="not_here",
            new_string="replacement",
            working_dir=tmp_path,
        )
        assert result.startswith("Error: old_string not found")
        # File is unchanged
        assert (tmp_path / "f.py").read_text() == "x = 1\n"

    def test_edit_errors_on_multiple_matches(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_edit_file

        (tmp_path / "f.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        result = _handle_edit_file(
            path="f.py",
            old_string="x = 1",
            new_string="x = 2",
            working_dir=tmp_path,
        )
        assert "appears 2 times" in result
        # File is unchanged
        assert (tmp_path / "f.py").read_text() == "x = 1\nx = 1\n"

    def test_edit_errors_on_missing_file(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_edit_file

        result = _handle_edit_file(
            path="nonexistent.py",
            old_string="anything",
            new_string="other",
            working_dir=tmp_path,
        )
        assert result.startswith("Error: FileNotFoundError")

    def test_edit_rejects_path_traversal(self, tmp_path):
        from theforge.runners.tool_runtime import _handle_edit_file

        result = _handle_edit_file(
            path="../outside.py",
            old_string="x",
            new_string="y",
            working_dir=tmp_path,
        )
        assert result.startswith("Error: path traversal rejected")


class TestDevAgentApiLoop:
    """Full AgentLoopManager tests exercising write_file and edit_file tools for dev use."""

    def _make_dev_manager(self, tmp_path, adapter) -> AgentLoopManager:
        from theforge.runners.tool_runtime import TOOL_REGISTRY

        profile = _make_profile(
            name="dev",
            allowed_tools=("write_file", "edit_file", "read_file", "bash", "glob", "grep"),
        )
        return AgentLoopManager(
            profile=profile,
            provider="openai",
            working_dir=tmp_path,
            tools=list(TOOL_REGISTRY.values()),
            provider_adapter=adapter,
        )

    def test_write_then_edit_then_finish(self, tmp_path):
        """Mock dev agent creates a file, edits it, then returns final text."""
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(
                            id="w1",
                            name="write_file",
                            arguments={"path": "src/app.py", "content": "x = 0\n"},
                        )
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            if call_count[0] == 2:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(
                            id="e1",
                            name="edit_file",
                            arguments={
                                "path": "src/app.py",
                                "old_string": "x = 0",
                                "new_string": "x = 42",
                            },
                        )
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[],
                text_output="Implementation complete.",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_dev_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "implement feature"}],
            tool_schemas=[],
        )
        assert result.success
        assert result.output == "Implementation complete."
        assert call_count[0] == 3
        # Verify the file was created and edited
        content = (tmp_path / "src" / "app.py").read_text()
        assert content == "x = 42\n"

    def test_write_file_result_fed_back_to_model(self, tmp_path):
        """write_file tool result is included in messages for the next turn."""
        received_messages: list[list[dict]] = []
        call_count = [0]

        def adapter(messages, tools):
            call_count[0] += 1
            received_messages.append(list(messages))
            if call_count[0] == 1:
                return LoopTurn(
                    tool_calls=[
                        ToolCallRequest(
                            id="w1",
                            name="write_file",
                            arguments={"path": "out.py", "content": "done\n"},
                        )
                    ],
                    text_output=None,
                    structured_data=None,
                    usage=_make_usage(),
                )
            return LoopTurn(
                tool_calls=[],
                text_output="done",
                structured_data=None,
                usage=_make_usage(),
            )

        manager = self._make_dev_manager(tmp_path, adapter)
        result = manager.run(
            initial_messages=[{"role": "user", "content": "go"}],
            tool_schemas=[],
        )
        assert result.success
        # Second call should have tool_results message containing write_file output
        second_msgs = received_messages[1]
        roles = [m["role"] for m in second_msgs]
        assert "tool_results" in roles
        tool_results_msg = next(m for m in second_msgs if m["role"] == "tool_results")
        assert any("out.py" in r["content"] for r in tool_results_msg["results"])
