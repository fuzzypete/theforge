---
name: Extract coord_preflight module
slug: extract-coord-preflight
depends_on: extract-coord-state
---

# Spec: Extract coord_preflight Module

## Goal

Extract preflight, complexity adaptation, and model escalation logic from
`coordinator.py` into `src/theforge/coord_preflight.py`. Zero behavioral change.

## What to Extract

- `_load_file_scope_contents`
- `_parse_preflight_verdict`
- `_parse_preflight_complexity`
- `_find_registry_info_for_profile`
- `_find_registry_key_for_profile`
- `_has_persistent_p1`
- `_escalate_dev_model`
- `_apply_complexity_adaptation`

## Import Graph

`coord_preflight.py` imports: `coord_state`, `config`, `runner`, `task`. No circular deps.

## Backward Compatibility

```python
from .coord_preflight import (
    _parse_preflight_verdict, _parse_preflight_complexity,
    _has_persistent_p1, _escalate_dev_model, _apply_complexity_adaptation
)
```

## Acceptance Criteria

1. `src/theforge/coord_preflight.py` exists with all listed symbols.
2. `make test` passes. `make lint` passes.
3. `coordinator.py` is smaller by at least 200 lines.
