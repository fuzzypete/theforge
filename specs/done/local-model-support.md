---
name: "Local model support — validate ollama/vllm via existing OpenAI adapter"
slug: local-model-support
pytest_target: tests/
---

# Local Model Support

## Problem

The runner_api.py OpenAI adapter already supports base_url for custom
endpoints. Local models via ollama/vllm expose OpenAI-compatible APIs.
No new adapter code is needed — but it's untested and there are edge
cases around tool calling support, cost tracking, and model capabilities.

## Solution

Validate and harden the existing OpenAI adapter path for local model use:

1. **Tool calling compatibility**: some local models don't support tool
   use. Detect this (400 error on first tool call) and fall back to
   text-only mode with explicit instructions to output structured text.

2. **Cost tracking**: local models should report $0.00. When base_url
   is set to a local endpoint (localhost, 127.0.0.1), set cost to 0
   regardless of token counts.

3. **Model registry**: add common local models (codestral, deepseek-coder,
   llama3.1, qwen2.5-coder) to MODEL_REGISTRY with capability scores
   and dev_capable flags.

4. **Documentation**: add a "Local Models" section to docs/ showing
   forge.yaml config for ollama and vllm.

5. **Smoke test**: extend `forge check-providers` to test local endpoints
   when configured.

## Acceptance Criteria

- [ ] OpenAI adapter works with ollama base_url for models with tool support
- [ ] Graceful fallback when model doesn't support tool calling
- [ ] Cost reported as $0.00 for local endpoints
- [ ] Common local models added to MODEL_REGISTRY
- [ ] `forge check-providers` tests local endpoints when configured
- [ ] Documentation for local model setup with ollama/vllm
- [ ] All existing tests pass
- [ ] New tests for local endpoint detection and cost zeroing
