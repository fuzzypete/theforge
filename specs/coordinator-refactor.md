---
name: Coordinator module refactor
slug: coordinator-refactor
---

# Spec: Coordinator Module Refactor

## Problem

`src/theforge/coordinator.py` is ~3,200 lines with 48+ top-level functions.
`tests/test_coordinator.py` is ~7,500 lines. Every sprint that touches the
coordinator produces merge conflicts because all logic and all tests live in
one file. This is the primary bottleneck for forge developing itself.

This spec is a **pure refactor** — zero behavioral change. All existing tests
must pass unchanged (except import paths). The public API surface must be
backward-compatible: callers import from `theforge.coordinator` and nothing
breaks.

## Target Module Structure

Split coordinator.py into focused modules inside `src/theforge/`:

### `src/theforge/coord_state.py`
Dataclasses and enums only. No logic, no subprocess calls.
- `Phase` (enum)
- `ReviewCycleMetadata` (dataclass)
- `CoordinatorState` (dataclass + properties)
- `CoordinatorResult` (dataclass)

### `src/theforge/coord_notify.py`
All notification logic: macOS, ntfy, human review.
- `_osa_quote`, `_notify`, `_escalate_notify`
- `_ntfy_done_notify`, `_ntfy_escalate_notify`, `_ntfy_publish`, `_ntfy_poll_reply`, `_ntfy_reply_url`
- `_is_remote_mode`, `_remote_human_review`, `_human_review`

### `src/theforge/coord_workspace.py`
Worktree lifecycle and merge machinery.
- `_fmt_age`, `_is_stale_worktree`, `_remove_worktree`
- `_create_workspace`, `_resolve_merge_conflicts`, `_merge_branch`

### `src/theforge/coord_gate.py`
Gate execution, dirty-worktree detection, auto-commit.
- `_is_gate_skip`, `_read_gate_decision`, `_run_gate`, `_run_gate_full`
- `_parse_dirty_files`, `_auto_commit_side_effects`, `_in_scope`

### `src/theforge/coord_preflight.py`
Preflight parsing, complexity adaptation, model escalation.
- `_load_file_scope_contents`, `_parse_preflight_verdict`, `_parse_preflight_complexity`
- `_find_registry_info_for_profile`, `_find_registry_key_for_profile`
- `_has_persistent_p1`, `_escalate_dev_model`, `_apply_complexity_adaptation`

### `src/theforge/coordinator.py` (slimmed, target ~1,200 lines)
Logging helpers, shell helper, diff helpers, the main state machine loop,
public entry points, audit log generation.

**Re-exports for backward compatibility:**
```python
from .coord_state import Phase, ReviewCycleMetadata, CoordinatorState, CoordinatorResult
from .coord_notify import _notify, _ntfy_publish, _is_remote_mode
from .coord_gate import _run_gate
```

## Import Graph (no circular deps)

```
coord_state     ← stdlib only
coord_notify    ← coord_state, config, runner
coord_gate      ← coord_state, config, runner
coord_preflight ← coord_state, config, runner, task
coord_workspace ← coord_state, config, runner, coord_notify, coord_gate
coordinator     ← all of the above + task, review, schemas
```

## Test File Split

Split `tests/test_coordinator.py` into focused files:

| New file | What moves there |
|---|---|
| `tests/test_coord_state.py` | CoordinatorState property tests |
| `tests/test_coord_notify.py` | ntfy, _human_review, _remote_human_review tests |
| `tests/test_coord_workspace.py` | workspace create/reuse/stale/merge tests |
| `tests/test_coord_gate.py` | gate run, dirty worktree, auto-commit tests |
| `tests/test_coord_preflight.py` | preflight parse, complexity, escalation tests |
| `tests/test_coordinator.py` | coordinator loop, run_task, audit log tests (remainder) |

## Acceptance Criteria

1. `make test` passes with 0 failures (same test count).
2. `make lint` passes.
3. `from theforge.coordinator import (CoordinatorResult, CoordinatorState, Phase, ReviewCycleMetadata, _fmt_duration, _is_remote_mode, _notify, _ntfy_publish, _run_gate, generate_audit_log, run_from_dev, run_from_review, run_task, set_log_level)` all succeed.
4. `coordinator.py` is under 2,100 lines after the split.
5. `test_coordinator.py` is under 2,000 lines after the split.
6. Each new `tests/test_coord_*.py` file is independently runnable: `pytest tests/test_coord_gate.py -v` works.

## Implementation Order

1. Create `coord_state.py` — move dataclasses/enums, add re-exports to coordinator.py
2. Create `coord_notify.py` — move notify functions, update coordinator.py imports
3. Create `coord_gate.py` — move gate functions, update coordinator.py imports
4. Create `coord_preflight.py` — move preflight functions, update coordinator.py imports
5. Create `coord_workspace.py` — move workspace/merge functions, update imports
6. Run `make fmt && make test` after each step
7. Split test_coordinator.py into new test files, update imports
8. Final `make fmt && make test`

## File Scope

(no restriction — this is a codebase-wide restructuring)
