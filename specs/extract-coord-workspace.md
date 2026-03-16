---
name: Extract coord_workspace module
slug: extract-coord-workspace
depends_on: extract-coord-notify
---

# Spec: Extract coord_workspace Module

## Goal

Extract worktree lifecycle and merge machinery from `coordinator.py` into
`src/theforge/coord_workspace.py`. Zero behavioral change.

## What to Extract

- `_fmt_age`
- `_is_stale_worktree`
- `_remove_worktree`
- `_create_workspace`
- `_resolve_merge_conflicts`
- `_merge_branch`

## Import Graph

`coord_workspace.py` imports: `coord_state`, `config`, `runner`, `coord_notify`, `coord_gate`.
One-way dep on coord_gate: `_resolve_merge_conflicts` calls `_run_gate` to verify
conflict resolution didn't break tests.

## Backward Compatibility

```python
from .coord_workspace import _create_workspace, _merge_branch, _resolve_merge_conflicts
```

## Acceptance Criteria

1. `src/theforge/coord_workspace.py` exists with all listed symbols.
2. `make test` passes. `make lint` passes.
3. `coordinator.py` is smaller by at least 200 lines.
