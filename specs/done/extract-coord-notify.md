---
name: Extract coord_notify module
slug: extract-coord-notify
depends_on: extract-coord-state
---

# Spec: Extract coord_notify Module

## Goal

Extract all notification logic from `coordinator.py` into a new
`src/theforge/coord_notify.py` module. Zero behavioral change.

## What to Extract

Move these to `coord_notify.py`:
- `_osa_quote`
- `_notify`
- `_escalate_notify`
- `_ntfy_done_notify`
- `_ntfy_escalate_notify`
- `_ntfy_publish`
- `_ntfy_poll_reply`
- `_ntfy_reply_url`
- `_is_remote_mode`
- `_remote_human_review`
- `_human_review`

## Import Graph

`coord_notify.py` imports: `coord_state`, `config`, `runner`. No circular deps.

## Backward Compatibility

Add re-exports to `coordinator.py`:
```python
from .coord_notify import _notify, _ntfy_publish, _is_remote_mode, _human_review
```

Update `tests/conftest.py` autouse fixture to also patch `theforge.coord_notify._notify`
and `theforge.coord_notify._ntfy_publish` so tests don't fire real notifications.

## Acceptance Criteria

1. `src/theforge/coord_notify.py` exists with all listed symbols.
2. `from theforge.coordinator import _notify, _ntfy_publish, _is_remote_mode` works.
3. `make test` passes. `make lint` passes.
4. `coordinator.py` is smaller by at least 200 lines.
