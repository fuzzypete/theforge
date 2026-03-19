---
name: Extract coord_gate module
slug: extract-coord-gate
depends_on: extract-coord-state
---

# Spec: Extract coord_gate Module

## Goal

Extract gate execution and dirty-worktree logic from `coordinator.py` into
`src/theforge/coord_gate.py`. Zero behavioral change.

## What to Extract

- `_is_gate_skip`
- `_read_gate_decision`
- `_run_gate_full`
- `_run_gate`
- `_parse_dirty_files`
- `_auto_commit_side_effects`
- `_in_scope`

## Import Graph

`coord_gate.py` imports: `coord_state`, `config`, `runner`. No circular deps.

## Backward Compatibility

```python
from .coord_gate import _run_gate, _run_gate_full, _parse_dirty_files, _auto_commit_side_effects
```

## Acceptance Criteria

1. `src/theforge/coord_gate.py` exists with all listed symbols.
2. `from theforge.coordinator import _run_gate` works.
3. `make test` passes. `make lint` passes.
4. `coordinator.py` is smaller by at least 150 lines.
