---
name: Add a provider setup decision guide
slug: docs-provider-setup-chooser
pytest_target: tests/
---

# Add a Provider Setup Decision Guide

## Problem

TheForge supports many provider configurations (CLI mode, API mode, mixed, local
models, multi-reviewer pools). Users face decision paralysis without guidance on
which setup fits their situation. "Many knobs" needs to become "pick one pattern."

## Acceptance criteria

- A new file docs/guides/choose-your-provider-setup.md exists
- It presents 4-5 named setup patterns, each with:
  - A short name (e.g., "Fastest start", "Budget-conscious", "Maximum coverage")
  - When to use it (one sentence)
  - Example forge.yaml profiles section
  - Approximate cost per story
  - Trade-offs (speed, cost, quality, privacy)
- Patterns include at minimum:
  - **Fastest start**: single Claude CLI for dev and review
  - **Budget-conscious**: cheaper dev model + stronger reviewer
  - **Maximum coverage**: multi-model review pool (3-4 reviewers)
  - **Local / privacy-first**: local model via Ollama/vLLM for dev or review
- The guide is linked from README, Getting Started, and CLI Reference
