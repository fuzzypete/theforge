---
name: "Multi-CLI support: Codex and Gemini runners"
slug: multi-cli
file_scope:
  - src/theforge/runner.py
  - src/theforge/config.py
  - tests/test_runner.py
  - tests/test_config.py
pytest_target: tests/
---

# Multi-CLI Support: Codex and Gemini Runners

## Problem

TheForge currently only supports `claude` as a CLI backend. The value of
multi-model review comes from independent model families — Claude reviewing
Claude has overlapping blind spots. Codex (OpenAI) and Gemini (Google)
provide genuinely independent perspectives.

Both CLIs are installed on this machine:

- **Codex:** `npx @openai/codex exec --full-auto -m <model> "prompt"` (stdin also accepted)
- **Gemini:** `gemini -p "prompt" --yolo -m <model> -o json`

## Requirements

### R1: CLI dispatch layer in runner.py

`run_agent()` already has a dispatch dict:

```python
runners = {
    "claude": _run_claude,
}
```

Add `_run_codex` and `_run_gemini` implementations following the same
pattern as `_run_claude`:

- Accept prompt via stdin (Codex supports this) or as a positional arg
- Run in a background thread with the 30s heartbeat (reuse the existing pattern)
- Return `AgentResult` with the same fields populated
- Handle `TimeoutExpired` and `FileNotFoundError` the same way

### R2: Codex runner (`_run_codex`)

Invocation:

```
npx @openai/codex exec --full-auto -m <model> -C <working_dir> "<prompt>"
```

Key differences from Claude:
- Codex uses `npx @openai/codex exec` not a bare `codex` command
- `--full-auto` enables sandboxed auto-execution (like Claude's allowed tools)
- `-C <dir>` sets the working directory (instead of `cwd=` on subprocess)
- `--json` outputs JSONL events to stdout — but for simplicity, just capture
  stdout as text output. Do NOT try to parse JSONL events.
- `-o <file>` writes the last message to a file — use this with a temp file
  to get the final agent response
- No `--output-format json` equivalent that gives cost — set `cost_usd=0.0`
- No session resume — set `session_id=None` always
- Prompt can be passed as a positional arg (quoted) or via stdin with `-`
- `allowed_tools` from the profile should be ignored (Codex has its own
  permission model via `--full-auto`)
- Codex does NOT need `CLAUDECODE` unset from env

### R3: Gemini runner (`_run_gemini`)

Invocation:

```
gemini -p "<prompt>" --yolo -m <model> -o json
```

Key differences from Claude:
- `gemini` is the command (aliased via npx, but callable directly)
- `-p "<prompt>"` passes prompt as a flag, NOT stdin
- `--yolo` auto-approves all actions (equivalent to allowed_tools)
- `-o json` outputs structured JSON
- `-m <model>` selects the model (e.g., `gemini-2.5-pro`)
- `--approval-mode yolo` is equivalent to `--yolo`
- No session resume support
- No cost reporting — set `cost_usd=0.0`
- Working directory: use `cwd=` on `subprocess.run` (same as Claude)
- `allowed_tools` from the profile should be ignored (Gemini uses `--yolo`)
- Gemini does NOT need `CLAUDECODE` unset from env
- Prompt passed via `-p` flag, so no `input=` on subprocess.run

### R4: Update SUPPORTED_CLIS

In `config.py`, expand:

```python
SUPPORTED_CLIS: frozenset[str] = frozenset({"claude", "codex", "gemini"})
```

All existing config validation (CLI checks in load_config) will
automatically support the new CLIs — no other config.py changes needed.

### R5: Output parsing resilience

Each runner should follow this pattern for output:
1. Try to parse stdout as JSON → extract meaningful fields
2. If JSON parsing fails → use raw stdout as `output`
3. If stdout is empty → use stderr as `output`
4. Always return a valid `AgentResult`, never raise

For Codex with `-o <file>`: read the output file for the final message,
fall back to stdout if the file doesn't exist.

### R6: Tests

Add to `tests/test_runner.py`:

- `TestRunCodex` class mirroring the existing Claude tests:
  - `test_codex_success`: mock subprocess returns 0, verify AgentResult fields
  - `test_codex_timeout`: mock raises TimeoutExpired
  - `test_codex_not_found`: mock raises FileNotFoundError
  - `test_codex_command_structure`: verify the command list includes
    `npx`, `@openai/codex`, `exec`, `--full-auto`, `-m`, model

- `TestRunGemini` class:
  - `test_gemini_success`: mock subprocess returns 0, verify AgentResult fields
  - `test_gemini_timeout`: mock raises TimeoutExpired
  - `test_gemini_not_found`: mock raises FileNotFoundError
  - `test_gemini_command_structure`: verify command includes
    `gemini`, `-p`, `--yolo`, `-m`, model, `-o`, `json`

- `TestRunAgentPool` updates:
  - `test_pool_mixed_clis`: pool with one Claude and one Gemini profile,
    verify both dispatch correctly

### R7: Update default config template

In `generate_default_config()`, add a commented-out example showing
how to configure Codex and Gemini profiles:

```yaml
# Multi-CLI review pool example:
# review_pool:
#   - name: claude-reviewer
#     cli: claude
#     model: opus
#     budget_usd: 1.00
#   - name: codex-reviewer
#     cli: codex
#     model: o4-mini
#     budget_usd: 1.00
#   - name: gemini-reviewer
#     cli: gemini
#     model: gemini-2.5-pro
#     budget_usd: 1.00
# synthesis:
#   cli: claude
#   model: opus
#   budget_usd: 1.00
```

## Out of scope

- Actually running Codex/Gemini in tests (all subprocess calls are mocked)
- Cost tracking for Codex/Gemini (hardcoded to 0.0 for now)
- Session resume for Codex/Gemini
- Tool mapping between CLIs (each CLI manages its own tools)
- Parallel execution in `run_agent_pool` (remains sequential)

## Acceptance criteria

1. `forge run --dry-run` works with `cli: codex` and `cli: gemini` in config
2. All existing tests pass unchanged
3. New runner tests cover success, timeout, not-found for both CLIs
4. `SUPPORTED_CLIS` contains all three
5. Config validation rejects unknown CLIs, accepts all three
