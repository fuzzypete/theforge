---
title: "Route Ollama local models through OpenAI-compatible runner"
type: story
priority: p2
milestone: M3
---

## Context

The `--dev-model` flag accepts `provider/model@base_url` format. When `provider` is `ollama`
(or `base_url` points to localhost), the current code routes the request through the `claude`
CLI (Claude Code shell). Ollama models don't speak Anthropic's tool protocol, so they produce
0 tool calls and make no code changes.

Ollama already exposes an OpenAI-compatible API at `localhost:11434/v1`, including function
calling. Modern Ollama models (qwen2.5-coder, llama3, mistral, etc.) support OpenAI-format
tool schemas. The existing `runner_api.py` OpenAI-compatible loop already works correctly for
DeepSeek and OpenAI.

## Acceptance Criteria

1. When `--dev-model` specifies an `ollama` provider or a `base_url` containing `localhost` or
   `127.0.0.1`, the runner uses the OpenAI-compatible API path (same as DeepSeek/Codex), not
   the `claude` CLI.
2. Tool calls (Read, Edit, Write, Bash, Glob, Grep) are delivered as OpenAI-format function
   schemas and the model's function call responses are parsed correctly.
3. A `forge run --dev-model ollama/qwen2.5-coder:14b@http://localhost:11434` invocation against
   a simple story produces at least one tool call (observable in the run log).
4. Cost for local model runs remains `$0.00` (existing behavior preserved).
5. No regression for cloud providers (openai, deepseek, anthropic).
