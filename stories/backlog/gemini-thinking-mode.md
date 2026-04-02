---
name: "Gemini thinking mode — thinking_budget support in API runner"
slug: gemini-thinking-mode
github_issue: 252
pytest_target: tests/
---

# Gemini Thinking Mode

## Problem

TheForge's Gemini API runner never sets `ThinkingConfig`, so thinking tokens are
neither enabled nor disabled explicitly — the model uses whatever its default is.
`reasoning_effort` is silently ignored for Gemini (both CLI and API paths). Users
cannot access Gemini's extended thinking capability, which meaningfully improves
review quality for complex stories.

## Goal

A `thinking_budget` field on Gemini API profiles that enables and controls
thinking mode. When set, thinking tokens are explicitly budgeted; when absent,
behavior is unchanged from today.

## Acceptance Criteria

- A `thinking_budget` integer field is accepted in Gemini API profile config
  (tokens, e.g. `thinking_budget: 8000`)
- When `thinking_budget` is set and > 0, thinking mode is active for that profile
- When `thinking_budget: 0`, thinking is explicitly disabled (Gemini supports this)
- When `thinking_budget` is absent, the model's default applies — no regression
  for existing configs
- Thinking mode works correctly in the single-shot API path (plain-text and
  structured-output calls)
- Thinking mode works correctly in the tool-use loop path, including finalization
  calls
- Thinking tokens are counted and included in cost estimation where pricing data
  exists
- `forge check-config` reflects `thinking_budget` when set on a profile
- Existing tests pass unchanged
- New tests cover: thinking_budget > 0 activates ThinkingConfig, thinking_budget
  0 disables it, absent thinking_budget leaves config unchanged

## Out of Scope

- CLI runner thinking support (Gemini CLI has no stable `--thinking-budget` flag;
  leave the existing silent-ignore behavior and note it in the runner)
- Anthropic extended thinking (separate concern, separate profile field)
- Auto-selecting thinking budget based on story complexity

## Notes

- `gemini-adapter-hardening` must ship first. When thinking is active, the model
  produces thought blocks before function calls, and the loop adapter must attach
  `thought_signature` from those thought blocks to subsequent function call parts
  or the API returns 400 INVALID_ARGUMENT. That fix lives in the hardening story.
- `ThinkingConfig` is set inside `GenerateContentConfig`. Both the single-shot
  path and the loop adapter's per-turn call and finalization call need it.
- Thinking token counts come from `usage_metadata.thoughts_token_count` (or
  similar field — verify against the SDK version in use).
- Pricing for thinking tokens differs from output tokens on some Gemini models;
  check the pricing table in `schema_utils.py` and add thinking-token rates if
  the SDK exposes them separately.
