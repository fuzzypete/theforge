---
name: "Local model adapter — aider and OpenAI-compatible local endpoints"
slug: local-model-adapter
pytest_target: tests/
---

## Problem

All current dev and review agents run against cloud APIs (Anthropic, OpenAI, Google, DeepSeek). This means:
1. Every DEV iteration costs real money — even when the plan is strong and the task is mechanical
2. No way to leverage local models (ollama, LM Studio, vLLM) for cheap first-pass dev
3. No path to aider integration, which has strong edit-apply tooling for local models

The cost-tiered generation vision calls for cheap models on DEV pass 1 when the plan is structured enough to guide implementation. The structured-plan-output spec (when shipped) provides the prerequisite — a machine-readable plan that a cheaper model can follow step-by-step.

## Solution

Two integration paths, both using existing infrastructure:

### Path 1: Aider adapter in runner.py

Aider is a CLI tool that takes a prompt and edits files. It supports local models via `--model` and cloud models. It handles its own file editing (edit-apply loop), so the tool runtime isn't needed.

Add `_run_aider()` to `runner.py`:
- Invoke aider as a subprocess (same pattern as `_run_claude()`, `_run_codex()`)
- Pass the dev prompt as the initial message
- Map `working_dir` to aider's `--chat-mode code` with appropriate file context
- Capture output and exit code
- Parse cost from aider's output (it reports token usage)

Profile config:
```yaml
profiles:
  dev_cheap:
    cli: aider
    model: ollama/codellama:34b   # or any aider-supported model string
    budget_usd: 0.10              # local models are essentially free
    timeout_seconds: 300
    aider_args:                   # optional extra aider CLI args
      - "--no-auto-commits"
      - "--yes"
```

### Path 2: OpenAI-compatible local endpoints in runner_api.py

The API runner already supports any OpenAI-compatible endpoint via the `provider` + `base_url` config. Local model servers (ollama, LM Studio, vLLM) expose OpenAI-compatible APIs. This path requires:

1. Add `base_url` to profile config (already partially supported via provider config)
2. Handle capability differences: local models may not support tool use, structured output, or streaming in the same way
3. Graceful degradation: if tool use isn't supported, fall back to prompt-based file editing instructions
4. Token counting: local endpoints may not report usage — estimate from prompt/response length

Profile config:
```yaml
profiles:
  dev_local:
    provider: openai          # use OpenAI-compatible client
    base_url: "http://localhost:11434/v1"  # ollama
    model: codellama:34b
    budget_usd: 0.00          # free
    timeout_seconds: 600
    capabilities:
      tool_use: false         # model doesn't support function calling
      structured_output: false
```

### Cost-tiered dev strategy (enabled by this + structured-plan-output):

```yaml
dev_strategy:
  pass_1:
    profile: dev_local        # cheap local model for first implementation
    condition: structured_plan  # only when plan is structured YAML
  pass_2:
    profile: dev              # cloud model for fixes after review
```

This is future config — the adapter ships first, the strategy wiring comes after structured-plan-output.

### What ships in this spec:
1. `_run_aider()` in `runner.py` — aider CLI adapter
2. `base_url` support in API runner config for local endpoints
3. `capabilities` config for graceful degradation
4. Profile config for both paths
5. `forge check-providers` updated to test local endpoints

### What ships later:
- `dev_strategy` config (depends on structured-plan-output)
- Automatic fallback from local to cloud on failure
- Token usage estimation for local models

## Acceptance criteria:
- `_run_aider()` added to `runner.py`, invokes aider as subprocess
- Aider profile config accepted in forge.yaml (cli: aider, model, aider_args)
- `base_url` in profile config routes API runner to local endpoint
- `capabilities` config controls graceful degradation (tool_use, structured_output)
- `forge check-providers` tests local endpoints when configured
- Dev agent can run against a local model (either path) and produce file changes
- Cost tracking: aider output parsed for tokens, local API estimated
- Existing tests pass
- New tests for aider adapter (mocked subprocess), local endpoint config parsing, capability degradation
