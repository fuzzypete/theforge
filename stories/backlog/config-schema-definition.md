---
name: "Config schema definition — new types and role derivation logic"
slug: config-schema-definition
github_issue: 265
pytest_target: tests/
---

# Config Schema Definition

## Problem

Before building the new config loader, the target schema needs to be defined
as concrete types with tested derivation logic. The current `ForgeConfig` mixes
parsing, validation, and runtime concerns — the new schema should separate
"what the user writes" from "what the coordinator consumes."

## Goal

Define the new config schema types and a standalone `derive_roles()` function
that takes `models` + optional `overrides` and produces role assignments
(dev, review pool, synthesis, plan, preflight). This is the specification —
tested in isolation before any loader changes.

## Acceptance Criteria

- New schema types define the user-facing config shape: `models` list,
  `overrides` dict (keyed by role), top-level shorthands (`gate`, `on_approve`,
  `budget_usd`)
- `derive_roles(models, overrides)` produces complete role assignments from a
  models list, applying override replacements where present
- Derivation rules are tested: dev=first model, review pool=all models,
  synthesis=strongest, plan=strongest, preflight=dev model
- Override application is tested: override replaces only the targeted role,
  other roles remain derived
- Edge cases tested: single model (same model for all roles), empty overrides,
  override for a role not in the models list
- The derivation function produces objects compatible with existing `ForgeConfig`
  / `ModelProfile` internals — no coordinator changes needed downstream
- `make test` and `make lint` pass

## Out of Scope

- Loader changes (story 2)
- Rewriting forge.yaml or test fixtures (story 3)
- Deleting old code paths (story 4)
