---
name: Sprint spec dependencies
slug: sprint-dependencies
---

# Spec: Sprint Spec Dependencies

## Problem

Sprint runs specs sequentially but has no concept of dependencies between
specs. If spec A fails to merge, spec B starts anyway from stale main and
builds on broken foundations. For decomposed refactors (where spec 2 requires
spec 1's changes to be on main), this produces cascading failures.

## Solution

Add optional `depends_on` field to spec frontmatter. Sprint checks after each
merge: if a spec that another depends on did not merge cleanly, halt the sprint
before starting the dependent spec and surface the failure.

## Spec Frontmatter

```yaml
---
name: Extract coord_notify module
slug: extract-coord-notify
depends_on: extract-coord-state
---
```

`depends_on` accepts a single slug or a list of slugs.

## Sprint Behaviour

1. After each spec completes and is APPROVED, sprint eagerly merges it to main
   if any later spec in the manifest declares it as a dependency. This ensures
   dependent worktrees branch from main that includes the merged code.
2. Before starting each spec, sprint checks if all `depends_on` slugs merged
   successfully in this sprint run.
3. If any dependency did not merge (ESCALATED or SKIPPED), sprint marks the
   dependent spec as `SKIPPED (dependency failed)` and continues with
   remaining independent specs.
4. If `depends_on` is absent or empty, spec runs unconditionally (current
   behaviour preserved).
5. Eager merge only fires when a downstream dependent exists — specs with no
   dependents follow the existing auto_merge setting.

## Changes Required

### `src/theforge/task.py`
- Add `depends_on: list[str]` field to `TaskSpec` dataclass (default: `[]`)
- Parse `depends_on` from spec frontmatter (single string or list)

### `src/theforge/sprint.py`
- Track which slugs merged successfully during the sprint run
- After each APPROVE, check if any later spec depends on this slug — if yes,
  merge immediately before proceeding (regardless of `auto_merge` setting)
- Before each `run_task` call, check `task.depends_on` against merged set
- If dependency missing: log clear error, mark spec SKIPPED, continue sprint

### `src/theforge/config.py`
- No changes needed — dependency tracking is sprint-level, not config-level

## Acceptance Criteria

1. `TaskSpec.depends_on` parses correctly from frontmatter (str → list, list → list, missing → [])
2. Sprint eagerly merges an APPROVED spec when a later spec declares it as a dependency
3. Sprint skips a spec (not halts) when its dependency did not merge — remaining independent specs still run
4. Sprint proceeds normally when dependency merged successfully
5. Sprint proceeds normally when `depends_on` is absent
6. Eager merge does not fire for specs with no downstream dependents (respects `auto_merge`)
7. `make test` passes. `make lint` passes.

## File Scope

```
src/theforge/task.py
src/theforge/sprint.py
tests/test_task.py
tests/test_sprint.py
```
