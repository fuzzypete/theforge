---
name: "Move coord_*.py files into a coordinator/ package"
slug: coordinator-relocate-modules
pytest_target: tests/
---

# Move coord_*.py files into a coordinator/ package

## Problem

There are 10 `coord_*.py` files at the top level of `src/theforge/`. They all
belong to the coordinator subsystem but clutter the package namespace alongside
unrelated modules like `config.py`, `task.py`, and `runner.py`.

## Solution

Create `src/theforge/coordinator/` as a package. Move each existing
`coord_*.py` file into it, dropping the `coord_` prefix. Update all imports
across the codebase. `coordinator.py` stays in place for now — it will be
addressed in a follow-up story.

The package `__init__.py` re-exports everything that `coordinator.py` and the
`coord_*.py` modules currently expose, so external imports don't break.

## Constraints

- Pure file moves + import updates — zero behavioral change.
- `coordinator.py` is NOT moved or modified beyond updating its imports from
  the relocated modules.
- No circular imports.
- No new dependencies.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] No `coord_*.py` files remain at the `src/theforge/` top level.
- [ ] All relocated modules live under `src/theforge/coordinator/`.
- [ ] `coordinator.py` still exists and all its public imports still work.
