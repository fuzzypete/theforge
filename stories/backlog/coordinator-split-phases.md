---
name: "Split coordinator phases into per-phase modules"
slug: coordinator-split-phases
pytest_target: tests/
depends_on: [coordinator-extract-entrypoints]
---

# Split coordinator phases into per-phase modules

## Problem

`phases.py` (formerly `coord_phases.py`) inside the coordinator package is
~1,436 lines containing every state-machine phase implementation. Stories that
touch one phase risk merge conflicts with unrelated phase changes.

## Solution

Split `phases.py` into a `phases/` subpackage with one module per phase:

```
src/theforge/coordinator/phases/
  __init__.py     — re-exports all phase functions
  plan.py         — plan + plan_review phases
  dev.py          — dev phase + retry logic
  validate.py     — validate phase
  review.py       — review + review_only phases
```

Remove or replace the old `phases.py` with the subpackage.

## Constraints

- Pure structural refactor — zero behavioral change.
- No circular imports.
- No new dependencies.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] No single phase module exceeds 500 lines.
- [ ] The old monolithic `phases.py` no longer exists.
