---
name: "Split config.py into a config/ package"
slug: split-config-package
pytest_target: tests/
---

# Split config.py into a config/ package

## Problem

`config.py` is 1,184 lines mixing model registry definitions, profile handling,
secret resolution, YAML loading, and default values. It is imported by nearly
every module, making it the highest blast-radius file in the codebase. Edits to
model definitions risk breaking profile resolution and vice versa.

## Solution

Convert `src/theforge/config.py` into a `src/theforge/config/` package.

### Target layout

```
src/theforge/config/
  __init__.py     — re-exports full public API (ForgeConfig, load_config, etc.)
  types.py        — dataclasses and type definitions
  models.py       — model registry, MODEL_REGISTRY
  profiles.py     — profile resolution, budget defaults
  secrets.py      — API key resolution, env var lookups
  load.py         — YAML loading, merge logic, validation
  defaults.py     — default config values, threshold constants
```

## Constraints

- Pure structural refactor — zero behavioral change.
- `from theforge.config import ForgeConfig, load_config` and all other existing
  public imports must continue to work.
- Highest blast-radius split — extra care on re-exports.
- No new dependencies.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] All existing public imports from `theforge.config` still work.
- [ ] No single file in `src/theforge/config/` exceeds 300 lines.
- [ ] The old `config.py` file no longer exists.
