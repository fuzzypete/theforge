---
name: "Gemini thinking mode — thinking_budget config support"
slug: gemini-thinking-mode
pytest_target: tests/
---

# Gemini thinking mode — thinking_budget config support

## Problem

TheForge's Gemini API runner never sets `ThinkingConfig`. Users cannot enable
Gemini's extended thinking, which improves review quality for complex stories.

## Acceptance criteria

- Gemini API profile config supports an integer `thinking_budget` field
- `thinking_budget > 0` enables Gemini `ThinkingConfig` with that token budget
- `thinking_budget: 0` explicitly disables thinking
- Absent `thinking_budget` preserves current model-default behavior
- Thinking mode works in both single-shot and tool-use loop paths
- Thinking tokens are counted in cost estimation
- `forge check-config` reflects `thinking_budget`
- Existing tests continue to pass

## Notes

- Prerequisite: `gemini-adapter-hardening` (thought_signature fix)
