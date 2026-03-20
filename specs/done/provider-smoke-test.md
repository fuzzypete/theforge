---
name: "forge check-providers — manual provider smoke test"
slug: provider-smoke-test
pytest_target: tests/
---

# Provider Smoke Test

## Problem

When a new provider is added or API credentials change, there's no quick way
to verify all configured providers are reachable and returning valid structured
output. We find out something is broken mid-run after spending budget on dev.

## Solution

`forge check-providers` — reads `forge.yaml`, fires a minimal review prompt at
each API-mode profile, and reports pass/fail + latency + cost per provider.
Not part of the test suite (no free token burns). Run manually when adding a
provider or after credential rotation.

## Design

### Command

```bash
forge check-providers           # test all API profiles in forge.yaml
forge check-providers --profile gemini-reviewer  # test one profile
```

### Test prompt

A minimal but realistic prompt — a one-line diff with a trivial change — that
exercises the full structured output path without triggering deep exploration:

```
Review this change: -    x = 1\n+    x = 2
Verdict: APPROVE. No findings.
```

Single-shot (no tools). Expects a valid `ReviewResult` back via the provider's
structured output mechanism.

### Output

```
[check-providers] forge.yaml: 4 API profiles found

  claude-reviewer    anthropic  claude-opus-4-6         ✓  1.2s  $0.003
  codex-reviewer     openai     gpt-5.1-codex-mini      ✓  3.4s  $0.001
  gemini-reviewer    google     gemini-2.5-flash        ✓  0.8s  $0.000
  deepseek-reviewer  deepseek   deepseek-chat           ✓  1.1s  $0.000

[check-providers] 4/4 passed
```

Failures show the error inline:

```
  deepseek-reviewer  deepseek   deepseek-chat           ✗  AuthenticationError: invalid key
```

### Implementation

- New `cmd_check_providers` in `cli.py`
- Reuses `run_api_agent` with `allowed_tools=[]` (single-shot mode)
- Runs providers in parallel (ThreadPoolExecutor)
- Validates response parses as ReviewResult with verdict field present
- Reports wall-clock latency and cost per provider

## Acceptance Criteria

- [ ] `forge check-providers` reads all API profiles from forge.yaml
- [ ] Each profile is tested in parallel with a minimal prompt
- [ ] Pass: structured output received with valid verdict field
- [ ] Fail: error message shown inline, exit code 1
- [ ] Latency and cost reported per provider
- [ ] `--profile <name>` tests a single profile
- [ ] No token cost beyond the minimal test prompts (~100 tokens each)
- [ ] New tests mock the API calls — no real provider hits in CI
