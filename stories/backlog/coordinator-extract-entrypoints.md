---
name: "Extract coordinator entrypoints and loop into the package"
slug: coordinator-extract-entrypoints
pytest_target: tests/
depends_on: [coordinator-relocate-modules]
---

# Extract coordinator entrypoints and loop into the package

## Problem

After the module relocation, `coordinator.py` is still ~2,782 lines containing
the public entry points, the main coordinator loop, and residual glue. It needs
to shrink to a thin facade.

## Solution

Extract the remaining logic from `coordinator.py` into focused modules inside
`src/theforge/coordinator/`:

- **`entrypoints.py`** — public entry points and their setup logic.
- **`loop.py`** — the main coordinator loop and helpers it calls that aren't
  already in other modules.

`coordinator.py` becomes a thin facade that re-exports the public API from the
package. It can be deleted once external callers are updated in the cleanup
story.

## Constraints

- Pure structural refactor — zero behavioral change.
- All existing public imports from `theforge.coordinator` must continue to work.
- No circular imports.
- No new dependencies.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] `coordinator.py` is under 200 lines (re-exports + minimal glue).
- [ ] All existing public imports from `theforge.coordinator` still work.
- [ ] No new module exceeds 800 lines.
