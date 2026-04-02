---
name: "Config dead code removal — delete old schema paths"
slug: config-dead-code-removal
github_issue: 268
pytest_target: tests/
---

# Config Dead Code Removal

## Problem

After the new loader is live and dogfooded, the old config parsing paths are
dead code: `smart_config_models` derivation, `profiles` parsing, `agents`
parsing, `model_name` compat shim. Dead code creates confusion for future
contributors and agents working in the codebase.

## Goal

Remove all old config parsing paths that are no longer reachable. Clean up
types that only existed to support the old schema.

## Acceptance Criteria

- `smart_config_models` derivation logic is removed from `config/load.py`
- `profiles` parsing (dev, preflight, synthesis, review_pool sections) is
  removed from `config/load.py`
- `agents` parsing is removed from `config/load.py`
- `model_name` backward-compat mapping is removed
- Dead types in `config/types.py` that only supported old schema are removed
- No test references old schema field names
- `make test` and `make lint` pass
- `forge check-config` still works correctly

## Out of Scope

- Any behavioral changes — this is pure deletion
- Changing `ForgeConfig` / `ModelProfile` runtime types (only removing
  dead input-side types)
