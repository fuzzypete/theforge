---
name: "Config normalization — consistent model specification across all sections"
slug: config-normalization
---

# Config normalization — consistent model specification across all sections

## Problem

`forge.yaml` uses different field names for the same concept depending on the section. In `plan:`, `model` means the CLI binary and `model_name` means the model identifier. In `profiles.dev`, `model` means the model identifier. Same key, opposite meaning. This has caused real bugs repeatedly.

Additionally, `load_config` is silent about invalid configurations — missing API keys, unknown CLI names, empty review pools with adaptive enabled — all fail at runtime instead of on load.

## Goal

All sections use the same fields (`cli`, `model`, `provider`) with the same semantics. `model_name` is deprecated (accepted on load, mapped to `model`, warning emitted). Invalid configurations raise errors at load time, not at runtime.
