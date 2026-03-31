---
name: "Config normalization cleanup — model_name deprecation + pool/adaptive cross-check"
slug: config-normalization-cleanup
pytest_target: tests/
---

# Config normalization cleanup

## Problem

Two acceptance criteria from the original config normalization story (#152) did not ship:

1. `model_name` under `plan:` in forge.yaml is now an unknown field — it is silently ignored on load. Users who still have `model_name:` in their config get no feedback; their plan model is simply unset.

2. When `assignment.enabled: true` and the review pool has no auth-valid agents, `load_config` does not raise — it silently proceeds. The sprint then fails at runtime with confusing errors instead of at startup.

(A third item — raising `ValueError` for missing API keys on non-plan profiles — was intentionally left as a warning. The adaptive pool's graceful-skip behavior is correct; `forge check-config` is the right place to surface this visibly.)

## Expected behavior

- `model_name` under `plan:` is read, mapped to `model`, and a deprecation warning is logged: `"plan.model_name is deprecated — use plan.model instead"`. The config loads successfully.
- If `assignment.enabled: true` and every agent in the review pool fails `check_agent_auth`, `load_config` raises `ValueError` with a clear message identifying the pool and the auth gap.

## Acceptance criteria

- A forge.yaml with `plan.model_name: some-model` loads without error and uses `some-model` as `plan.model`
- The deprecation warning is logged at WARNING level
- A forge.yaml with `assignment.enabled: true` and a pool of agents all missing their API keys raises `ValueError` at load time
- The error message names the agents that failed auth
- All existing tests pass
- New tests cover both behaviors
