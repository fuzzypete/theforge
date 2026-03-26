"""Provider dispatch tables for single-shot and loop-mode runners.

Each provider's implementation lives in its own module:
  - runner_openai.py    — OpenAI (Chat Completions + Responses API)
  - runner_anthropic.py — Anthropic
  - runner_google.py    — Google Gemini
  - runner_deepseek.py  — DeepSeek (OpenAI-compatible)

This module re-exports the symbols that api.py and tool_runtime.py reference
for backward compatibility, and builds the PROVIDER_RUNNERS / _LOOP_RUNNERS
dispatch dicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from theforge.agent_types import AgentResult
from theforge.runners.runner_anthropic import _run_anthropic, _run_loop_anthropic
from theforge.runners.runner_deepseek import _run_deepseek, _run_loop_deepseek
from theforge.runners.runner_google import (
    _run_google,
    _run_loop_google,
)
from theforge.runners.runner_openai import _run_loop_openai, _run_openai

if TYPE_CHECKING:
    pass

PROVIDER_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_openai,
    "anthropic": _run_anthropic,
    "google": _run_google,
    "deepseek": _run_deepseek,
}

_LOOP_RUNNERS: dict[str, Callable[..., AgentResult]] = {
    "openai": _run_loop_openai,
    "anthropic": _run_loop_anthropic,
    "google": _run_loop_google,
    "deepseek": _run_loop_deepseek,
}
