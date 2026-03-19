---
name: "Complexity-based timeouts for dev and plan phases"
slug: complexity-based-timeouts
pytest_target: tests/
---

# Complexity-Based Timeouts

## Problem

Forge uses a single flat `timeout_seconds` for the dev profile regardless
of spec complexity. Preflight already classifies specs as `small`, `medium`,
or `large` — but that signal is never used to adjust how long the dev agent
gets to run. The result:

- **Small specs time out too generously** — wasted wall-clock time waiting
  for agents that finished in 60s
- **Large specs time out too aggressively** — complex implementations get
  killed mid-commit, leaving dirty worktrees and forced escalations

The flat timeout is a blunt instrument. The fix is to use the complexity
signal already in `CoordinatorState` to select the appropriate timeout.

## Solution

Add per-complexity timeout overrides to the `DevProfile` config. The
coordinator selects the timeout at DEV start based on `state.preflight_complexity`.

### forge.yaml config

```yaml
profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 50.00
    timeout_seconds: 600          # default / small
    timeout_medium_seconds: 900   # medium complexity
    timeout_large_seconds: 1800   # large complexity
```

All three fields are optional. If a complexity-specific override is absent,
falls back to `timeout_seconds`. This is fully backward-compatible — existing
forge.yaml files with only `timeout_seconds` work unchanged.

### Config change (`src/theforge/config.py`)

Add optional fields to `DevProfile` (or equivalent profile dataclass):

```python
@dataclass(frozen=True)
class DevProfile:
    ...
    timeout_seconds: int = 600
    timeout_medium_seconds: int | None = None
    timeout_large_seconds: int | None = None
```

Add a helper method:

```python
def timeout_for_complexity(self, complexity: str | None) -> int:
    """Return the appropriate timeout for a given preflight complexity."""
    if complexity == "large" and self.timeout_large_seconds is not None:
        return self.timeout_large_seconds
    if complexity == "medium" and self.timeout_medium_seconds is not None:
        return self.timeout_medium_seconds
    return self.timeout_seconds
```

### Coordinator change (`src/theforge/coordinator.py` or `coord_phases.py`)

At DEV invocation, replace the flat `config.profiles.dev.timeout_seconds`
with:

```python
dev_timeout = config.profiles.dev.timeout_for_complexity(
    state.preflight_complexity
)
```

Pass `dev_timeout` to `run_agent(...)` instead of the hardcoded profile
timeout.

### Plan phase (`src/theforge/coordinator.py`)

Apply the same pattern to the PLAN phase. Add to `PlanConfig`:

```python
@dataclass(frozen=True)
class PlanConfig:
    ...
    timeout: int = 600
    timeout_medium: int | None = None
    timeout_large: int | None = None
```

With the same `timeout_for_complexity()` helper pattern, or a standalone
function in `coord_util.py`:

```python
def resolve_timeout(base: int, medium: int | None, large: int | None,
                    complexity: str | None) -> int:
    if complexity == "large" and large is not None:
        return large
    if complexity == "medium" and medium is not None:
        return medium
    return base
```

### Log output

When a complexity override is active, log it:

```
[forge]   Dev timeout: 1800s (large complexity)
```

When falling back to default:

```
[forge]   Dev timeout: 600s
```

### forge.yaml defaults for theforge

```yaml
profiles:
  dev:
    timeout_seconds: 600
    timeout_medium_seconds: 900
    timeout_large_seconds: 1800

plan:
  timeout: 600
  timeout_medium: 900
  timeout_large: 1800
```

## Acceptance Criteria

- [ ] `DevProfile` has `timeout_medium_seconds` and `timeout_large_seconds`
      optional fields (default `None`)
- [ ] `timeout_for_complexity()` returns the right value for small/medium/large/None
- [ ] Coordinator uses complexity-resolved timeout at DEV invocation
- [ ] PLAN phase uses complexity-resolved timeout
- [ ] Falls back to `timeout_seconds` when complexity-specific field is absent
- [ ] Log line shows resolved timeout and complexity when override is active
- [ ] `load_config()` parses new fields from forge.yaml
- [ ] forge.yaml updated with medium/large overrides for dev and plan
- [ ] Existing configs without new fields work unchanged (backward compat)
- [ ] New tests: `timeout_for_complexity` with all complexity values + None
- [ ] New tests: coordinator passes resolved timeout to run_agent
- [ ] New tests: plan phase passes resolved timeout
- [ ] All existing tests pass
