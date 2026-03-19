---
slug: deepseek-provider
priority: low
---

# DeepSeek API provider support

DeepSeek R1 and V3 expose an OpenAI-compatible API with tool use support.
Today they technically work via `provider: openai` + `base_url`, but that's
a hack — cost tracking uses OpenAI pricing, the model isn't in the pricing
table, and there's no provider-specific handling for DeepSeek's quirks
(reasoning tokens, tool-use reliability, context limits).

I want first-class `provider: deepseek` support so I can drop it into a
review pool alongside Claude and Codex without workarounds.

## Acceptance criteria

1. `provider: deepseek` recognized in forge.yaml profiles
2. Pricing table includes deepseek-r1 and deepseek-v3
3. `DEEPSEEK_API_KEY` env var / secrets support
4. Base URL defaults to `https://api.deepseek.com` when not overridden
5. Reasoning model detection covers DeepSeek R1 only (no temperature=0); V3/V3.2 support temperature normally
6. Existing tests pass; new test for deepseek profile parsing
