---
name: "forge init — scaffolded config and anti-pattern warnings"
slug: smart-defaults
pytest_target: tests/
---

# forge init

## Problem

New users have no guided entry point. They must write forge.yaml from scratch
or copy someone else's config without understanding why each choice was made.
Even with the simplified two-tier schema (`models` + `overrides`), there's no
command that generates a working starting config with documented rationale.

Additionally, misconfigured forge.yaml files (review model weaker than dev,
single-provider review pool, excessive retry limits) are only caught at
runtime when money is already being spent.

## Goal

`forge init` scaffolds a commented forge.yaml using the simplified schema.
Config parsing emits warnings for known anti-pattern configurations.

## Acceptance Criteria

- `forge init` creates a forge.yaml with the simplified schema, inline
  comments explaining model tiering rationale, and sensible defaults
- `forge init --minimal` creates the smallest working config (project +
  models + gate)
- `forge init` refuses to overwrite existing forge.yaml unless `--force`
- Config parsing emits warnings (logged, not blocking) for:
  - Single-provider review pool (correlated blind spots)
  - max_dev_iterations > 5 (diminishing returns)
  - max_review_cycles > 3 (same)
  - Plan review disabled (high-leverage phase unguarded)
- All existing tests pass

## Out of Scope

- Config schema design (solved by config-simplification epic)
- Built-in default values for missing config keys (already implemented)
- Model tiering logic (solved by `derive_roles()` in config-schema-definition)

## Notes

- This story depends on the config-simplification epic (#264) landing first —
  `forge init` should scaffold the new schema, not the old one.
- The scaffolded config should use the two-tier format: `models` list at top,
  `overrides` only if the user asked for provider-specific reviewers via
  `--providers`.
