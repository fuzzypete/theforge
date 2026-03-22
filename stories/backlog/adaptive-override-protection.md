---
name: "Adaptive assignment — fix override protection for all roles"
slug: adaptive-override-protection
pytest_target: tests/
---

# Adaptive Override Protection

## Problem

Two bugs in adaptive model assignment override handling:

### 1. Default review_pool treated as explicit (#114)
When forge.yaml has no explicit review_pool, the config loader falls back to
DEFAULT_REVIEW_PROFILE. The coordinator sees a non-empty config.review_pool and
marks it as an explicit override, silently ignoring adaptive code_reviewers.
Adaptive review is effectively disabled for classic configs.

### 2. Planner override not protected (#115)
The coordinator wires _adaptive.planner into PLAN when assignment is enabled
but never checks if the user explicitly configured plan.model in forge.yaml.
Unlike dev/preflight/review_pool, the planner has no override protection.

## Acceptance criteria

- Default/fallback review_pool is not treated as an explicit override
- User-configured review_pool in forge.yaml IS treated as explicit
- Explicit plan.model in forge.yaml is preserved when adaptive is enabled
- Adaptive planner only applies when plan config is default/unset
- Test: classic config with no review_pool gets adaptive reviewers
- Test: explicit review_pool in forge.yaml is preserved
- Test: explicit plan.model is preserved
- All existing tests pass
