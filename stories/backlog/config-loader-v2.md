---
name: "Config loader v2 — new schema parser with old-key rejection"
slug: config-loader-v2
github_issue: 266
pytest_target: tests/
---

# Config Loader v2

## Problem

The config loader in `config/load.py` supports multiple overlapping input
formats (`profiles`, `smart_config_models`, `agents`) with a fallback cascade.
The new schema needs a loader that accepts only the simplified format and
rejects old keys with actionable error messages.

## Goal

A new loader path in `config/load.py` that parses the two-tier schema
(`models` + `overrides`) and produces a `ForgeConfig` using the derivation
logic from config-schema-definition. Old keys trigger clear errors.

## Acceptance Criteria

- The loader accepts the new schema: `models`, `overrides`, `gate`, `on_approve`,
  `budget_usd` as top-level fields alongside existing structured sections
- `gate` as a top-level string is shorthand for `validation.gate_command`
- `on_approve` as a top-level string is shorthand for `workspace.on_approve`
- `budget_usd` as a top-level number sets the default dev budget
- `profiles`, `smart_config_models`, and `agents` keys are rejected with errors
  that name the replacement (e.g. "'profiles' is no longer supported — use
  'models' instead")
- The loader uses `derive_roles()` from config-schema-definition to produce
  role assignments
- `forge check-config` displays derived assignments and which overrides were
  applied
- Operational sections (`workspace`, `validation`, `conventions`, `retry`,
  `hooks`, `notifications`, `sprint`) parse unchanged
- `make test` and `make lint` pass

## Out of Scope

- Rewriting theforge's forge.yaml (story 3)
- Updating test fixtures (story 3)
- Deleting old loader code (story 4 — old path is dead but still present)
