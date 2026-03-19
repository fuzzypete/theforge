---
name: "API-mode dev agent — run dev via HTTP API instead of CLI subprocess"
slug: api-mode-dev
pytest_target: tests/
---

# API-Mode Dev Agent

## Problem

The dev agent currently requires a CLI subprocess (claude, codex, gemini).
This means:

1. **No local models for dev** — Ollama, vLLM, LM Studio all expose OpenAI-
   compatible APIs but have no CLI. They can review but not implement.
2. **No DeepSeek for dev** — DeepSeek V3 is a strong coder but API-only.
3. **No fine-tuned models** — custom models behind API endpoints can't dev.
4. **Adaptive model assignment is limited** — preflight can only assign from
   3 CLI options; with API dev, it could pick from any provider.

The API agent loop infrastructure already exists for reviewers: `AgentLoopManager`
with tool use, iteration/time nudges, forced finalization, and budget tracking.
It just needs to be wired as a dev runner.

## Solution

Allow `provider:` in the dev profile (same as review profiles). When the dev
profile uses `provider:` instead of `cli:`, the coordinator invokes the API
agent loop with write tools enabled (Read, Edit, Write, Bash, Glob, Grep).

## forge.yaml config

```yaml
profiles:
  dev:
    provider: openai                    # API mode
    model: codellama:70b
    base_url: http://localhost:11434/v1  # Ollama
    budget_usd: 0.00
    timeout_seconds: 1800
    max_iterations: 100
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
```

Or for DeepSeek:

```yaml
  dev:
    provider: deepseek
    model: deepseek-chat
    budget_usd: 5.00
    timeout_seconds: 900
    max_iterations: 50
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
```

CLI mode continues to work exactly as before. This is additive.

## Implementation

### `src/theforge/runner_api.py`

- Add dev-capable entry point: `run_dev_api(prompt, profile, secrets, cwd)` that
  calls `AgentLoopManager` with the full tool set (including Edit, Write)
- The tool runtime already supports all tools; this is just wiring
- Dev prompt is passed as the initial user message (same as CLI mode)
- Return `AgentResult` with cost, duration, model_usage (same shape as CLI)

### `src/theforge/runner.py`

- In `invoke_agent()`, check if dev profile has `provider` set
- If yes, route to `run_dev_api()` instead of CLI subprocess
- Handoff extraction: parse the agent's final message for the structured
  handoff YAML block (same format CLI agents produce)

### `src/theforge/coord_phases.py`

- No changes needed — `_run_dev_phase` already calls `invoke_agent()` and
  processes the result generically. API dev results have the same shape.

### `src/theforge/tool_runtime.py`

- Verify Write and Edit tools work in the dev agent's worktree CWD
- These tools already exist but may need CWD scoping validation

## Acceptance criteria

- [ ] Dev profile with `provider: openai` routes through API agent loop
- [ ] Dev profile with `provider: deepseek` routes through API agent loop
- [ ] API dev agent receives full tool set (Read, Edit, Write, Bash, Glob, Grep)
- [ ] API dev agent can create files, edit files, and run commands in worktree
- [ ] Handoff YAML is extracted from the agent's final response
- [ ] Cost tracking works for API dev (token-level breakdown in audit)
- [ ] Iteration nudge fires at 80% of max_iterations
- [ ] Time nudge fires at 80% of wall-clock timeout
- [ ] CLI dev profiles continue to work unchanged (backward compatible)
- [ ] `base_url` works for local model servers (Ollama, vLLM)
- [ ] Unit tests mock the API call and verify tool dispatch
- [ ] All existing tests pass
