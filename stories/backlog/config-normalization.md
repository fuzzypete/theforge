---
name: "Config normalization — consistent model specification across all sections"
slug: config-normalization
pytest_target: tests/
---

# Config Normalization

## Problem

`forge.yaml` has three different ways to specify a model depending on which
section you're in:

| Section | CLI binary field | Model name field |
|---------|-----------------|-----------------|
| `profiles.dev` | `cli:` | `model:` |
| `plan:` | `model:` | `model_name:` |
| `agents[]` | `cli:` | `model:` |
| `review_pool[]` | `cli:` or `provider:` | `model:` |

`plan.model` means the CLI binary. `profiles.dev.model` means the model name.
Same key, opposite meaning. This has caused real bugs in every session —
including `cli=None` being hardcoded in `AgentDef.to_model_profile` (fixed
twice), and hand-editing plan config with the wrong field names.

Additionally, `load_config` is silent about bad combinations:
- API-only agents with no API key configured → fails at runtime
- Plan reviewer is same model as planner (self-review, issue #130)
- `provider: ollama` silently routes to wrong protocol
- `smart_config_models` fallback overrides explicit config without logging

## Solution

### Unified model spec

Every section that references a model should use the same two fields:
- `cli:` — the CLI binary name (`claude`, `codex`) OR omit for API-only agents
- `model:` — the model identifier passed to the CLI or API
- `provider:` — for API-only agents (`anthropic`, `openai`, `deepseek`, `google`)

Deprecate `model_name:` in `plan:`. Accept it for backward compat but map it
to `model:` on load and emit a deprecation warning.

### Loud validation on load

`load_config` should raise `ConfigError` (not silently proceed) when:
- `cli:` is set to an unknown value (not `claude`, `codex`, or registered CLIs)
- `provider:` agent has no corresponding API key in environment
- Plan reviewer pool contains the same cli+model as the planner (self-review)
- `review_pool` or `agents` is empty but `assignment.enabled: true`
- `budget_usd` is missing on any agent or profile

Emit warnings (not errors) for:
- `smart_config_models` overriding an explicitly configured model
- `model_name:` used (deprecated)
- `max_iterations` exceeds a reasonable ceiling (> 50)

### Backward compat

All existing valid configs continue to load. Normalization happens at parse
time — the rest of the codebase sees the unified representation.

## Acceptance criteria

- `plan.model_name` accepted but mapped to `plan.model` with deprecation warning
- `plan.model` consistently means the model identifier (not the CLI binary)
- `plan.cli` accepted as the CLI binary field (matching all other sections)
- `load_config` raises `ConfigError` on the bad combinations listed above
- `load_config` logs warnings for soft issues
- All existing tests pass
- New tests for each validation error and deprecation warning
- theforge's own `forge.yaml` updated to use normalized fields
