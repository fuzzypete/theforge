---
name: Extract coord_state module
slug: extract-coord-state
---

# Spec: Extract coord_state Module

## Goal

Extract dataclasses and enums from `coordinator.py` into a new
`src/theforge/coord_state.py` module. Zero behavioral change.

## What to Extract

Move these to `coord_state.py`:
- `Phase` (enum)
- `ReviewCycleMetadata` (dataclass)
- `CoordinatorState` (dataclass + all properties/methods)
- `CoordinatorResult` (dataclass)
- `StructuredLogger` (class)

## Backward Compatibility

Add re-exports to `coordinator.py`:
```python
from .coord_state import (
    Phase, ReviewCycleMetadata, CoordinatorState, CoordinatorResult, StructuredLogger
)
```

## Import Graph

`coord_state.py` imports only stdlib. No internal theforge imports.

## Acceptance Criteria

1. `src/theforge/coord_state.py` exists with all listed symbols.
2. `from theforge.coordinator import Phase, CoordinatorState, CoordinatorResult, ReviewCycleMetadata` works.
3. `make test` passes (0 failures). `make lint` passes.
4. `coordinator.py` is smaller by at least 150 lines.
