---
name: "Config dogfood — rewrite forge.yaml and test fixtures to new schema"
slug: config-dogfood-and-tests
github_issue: 267
pytest_target: tests/
---

# Config Dogfood and Tests

## Problem

After the new loader is wired, theforge's own forge.yaml and all test fixtures
still use the old schema. The system works but isn't dogfooding its own
simplified config. Tests that construct config objects directly may bypass
the new schema entirely.

## Goal

Rewrite theforge's forge.yaml to the new two-tier schema and update all test
config factories and fixtures to use the new format. Prove the new schema works
end-to-end on theforge itself.

## Acceptance Criteria

- TheForge's forge.yaml uses the new schema (`models` + `overrides`, no
  `profiles` or `smart_config_models`)
- The rewritten forge.yaml is under 40 lines (excluding comments and
  conventions)
- All test fixtures and config factory helpers produce configs in the new
  schema format
- `forge check-config` on the new forge.yaml shows correct derived assignments
- `make test` passes with the new config — no tests rely on old schema paths
- `make lint` passes

## Out of Scope

- Deleting old loader code (story 4)
- Changing coordinator runtime behavior
