---
name: "forge check-config — show effective config and surface problems before running"
slug: forge-check-config
pytest_target: tests/
depends_on: [config-normalization]
---

# forge check-config

## Problem

Config state is opaque. Users repeatedly ask "what model is actually being used
for X?" and "what's in my review pool?" — and can only answer by reading 100
lines of YAML and tracing through load_config logic. Problems (missing keys,
wrong providers, self-review) only surface when a story runs and something
breaks.

## Solution

A `forge check-config` command that loads `forge.yaml`, runs all validation,
and prints the effective configuration — what will actually execute.

### Output format

```
forge check-config [forge.yaml]

Project: theforge
Budget:  $15.00/story

PHASES
  preflight   codex / gpt-5.4          timeout=300s  budget=$1.00
  plan        claude / claude-opus-4-5  timeout=600s  budget=$3.00
  dev         codex / gpt-5.4          timeout=900s  budget=$50.00

REVIEW POOL
  codex-reviewer       codex / gpt-5.4          role=correctness  budget=$5.00  ✓ auth
  gemini-reviewer      google / gemini-2.5-flash role=edge-cases   budget=$1.00  ✓ auth
  deepseek-reviewer    deepseek / deepseek-chat  role=patterns     budget=$1.00  ✗ no DEEPSEEK_API_KEY

PLAN REVIEWERS
  codex-plan-reviewer  codex / gpt-5.4          budget=$2.00  ✓ auth

AGENTS (adaptive pool)
  codex-cli    codex / gpt-5.4          tier=mid     ✓ auth
  codex-strong codex / gpt-5.4          tier=strong  ✓ auth
  claude-sonnet claude / claude-sonnet-4-5 tier=mid  ✓ auth

WARNINGS
  ⚠ deepseek-reviewer: DEEPSEEK_API_KEY not set — will be skipped at runtime

SETTINGS
  assignment:    enabled  (min=1, max=3, budget=$15.00/story)
  plan:          enabled
  plan_review:   enabled
  max_parallel:  3
  on_approve:    pr
```

### Auth checking

For each agent, check whether the required credential is available:
- `cli: claude` → check `claude` binary in PATH
- `cli: codex` → check `npx @openai/codex` available
- `provider: anthropic` → check `ANTHROPIC_API_KEY`
- `provider: openai` → check `OPENAI_API_KEY`
- `provider: deepseek` → check `DEEPSEEK_API_KEY`
- `provider: google` → check `GOOGLE_API_KEY` or `GEMINI_API_KEY`

### Exit codes

- `0` — config valid, no warnings
- `1` — config valid but has warnings (missing auth, deprecated fields)
- `2` — config invalid (would fail on `forge run`)

### Integration

`forge run` and `forge sprint run` should run the equivalent of
`check-config` validation at startup and print any warnings before proceeding.
Errors (exit 2) block the run.

## Acceptance criteria

- `forge check-config` prints effective config in the format above
- Auth check for each agent (CLI in PATH, API key in env)
- Exit code 0/1/2 as described
- `forge run` prints config warnings at startup
- `forge sprint run` prints config warnings at startup
- All existing tests pass
- New tests for check-config output and exit codes
