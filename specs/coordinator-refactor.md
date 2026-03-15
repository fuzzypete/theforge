---
name: Coordinator module refactor
slug: coordinator-refactor
---

# Spec: Coordinator Module Refactor

## Problem

`src/theforge/coordinator.py` is 2,913 lines with 48 top-level functions.
`tests/test_coordinator.py` is 7,518 lines. Every sprint that touches the
coordinator produces merge conflicts in test_coordinator.py because all
tests live in one file. This is now the primary bottleneck for forge
developing itself.

This spec is a **pure refactor** — zero behavioral change. All existing tests
must pass unchanged (except import paths). The public API surface must be
backward-compatible: callers import from `theforge.coordinator` and nothing
breaks.

## Target Module Structure

Split coordinator.py into 5 focused modules inside `src/theforge/`:

### `src/theforge/coord_state.py`
Dataclasses and enums only. No logic, no subprocess calls.

```
Phase (enum)
ReviewCycleMetadata (dataclass)
CoordinatorState (dataclass + properties)
CoordinatorResult (dataclass)
```

### `src/theforge/coord_notify.py`
All notification logic: macOS, ntfy, human review.

```
_osa_quote()
_notify()
_escalate_notify()
_ntfy_done_notify()
_ntfy_escalate_notify()
_ntfy_publish()
_ntfy_poll_reply()
_ntfy_reply_url()
_is_remote_mode()
_remote_human_review()
_human_review()
```

Imports: `coord_state`, `config`, `runner`

### `src/theforge/coord_workspace.py`
Worktree lifecycle and merge machinery.

```
_fmt_age()
_is_stale_worktree()
_remove_worktree()
_create_workspace()
_resolve_merge_conflicts()
_merge_branch()
```

Imports: `coord_state`, `config`, `runner`, `coord_notify`

### `src/theforge/coord_gate.py`
Gate execution, dirty-worktree detection, and auto-commit.

```
_is_gate_skip()
_read_gate_decision()
_run_gate()
_parse_dirty_files()
_auto_commit_side_effects()
```

Imports: `coord_state`, `config`

### `src/theforge/coord_preflight.py`
Preflight parsing, complexity adaptation, model escalation.

```
_load_file_scope_contents()
_parse_preflight_verdict()
_parse_preflight_complexity()
_find_registry_info_for_profile()
_find_registry_key_for_profile()
_has_persistent_p1()
_escalate_dev_model()
_apply_complexity_adaptation()
```

Imports: `coord_state`, `config`, `runner`, `task`

### `src/theforge/coordinator.py` (slimmed down, ~700 lines)
Logging helpers, shell helper, diff/handoff helpers, the main state machine,
public entry points, and audit log generation.

```
# Kept here:
LogLevel (re-export from runner or define)
set_log_level()
_fmt_duration()
_log(), _log_verbose(), _log_phase()
_run_shell()
_get_diff()
_get_handoff_content()
_run_pool_with_per_prompts()
_coordinator_loop()
run_task()
run_from_review()
run_from_dev()
generate_audit_log()
```

**Re-exports for backward compatibility** (so `from theforge.coordinator import X`
still works for all existing callers):

```python
from .coord_state import (
    Phase,
    ReviewCycleMetadata,
    CoordinatorState,
    CoordinatorResult,
)
from .coord_notify import _notify, _ntfy_publish, _is_remote_mode
from .coord_gate import _run_gate
```

These re-exports cover every symbol currently imported from coordinator by
`cli.py`, `sprint.py`, and the test suite.

## Import Graph (no circular deps)

```
coord_state   ← (no internal imports)
coord_notify  ← coord_state, config, runner
coord_gate    ← coord_state, config
coord_preflight ← coord_state, config, runner, task
coord_workspace ← coord_state, config, runner, coord_notify
coordinator   ← all of the above + task, review, schemas
```

## Test File Split

Split test_coordinator.py (~7,518 lines) into focused files:

| New file | What moves there | Approx lines |
|---|---|---|
| `tests/test_coord_state.py` | CoordinatorState property tests | ~150 |
| `tests/test_coord_notify.py` | ntfy, _human_review, _remote_human_review tests | ~400 |
| `tests/test_coord_workspace.py` | workspace create/reuse/stale/merge tests | ~800 |
| `tests/test_coord_gate.py` | gate run, dirty worktree, auto-commit tests | ~600 |
| `tests/test_coord_preflight.py` | preflight parse, complexity, escalation tests | ~500 |
| `tests/test_coordinator.py` | coordinator loop, run_task, audit log tests | ~1,500 |

**Rule:** A test class moves to the new file if its `from theforge.coordinator import`
statements would only need the target sub-module plus coordinator.py itself.

## Acceptance Criteria

1. `make test` passes with 0 failures before and after (same test count).
2. `make lint` passes (ruff, no new violations).
3. `from theforge.coordinator import (CoordinatorResult, CoordinatorState,
   Phase, ReviewCycleMetadata, _fmt_duration, _is_remote_mode, _notify,
   _ntfy_publish, _run_gate, generate_audit_log, run_from_dev,
   run_from_review, run_task, set_log_level)` — all succeed without error.
4. coordinator.py is under 800 lines after the split.
5. No new public API: every function moved to a sub-module is still a private
   `_name` function (or explicitly re-exported). Sub-modules are implementation
   details, not public API.
6. test_coordinator.py is under 2,000 lines after the split.
7. Each new `tests/test_coord_*.py` file is independently runnable:
   `pytest tests/test_coord_gate.py -v` works without importing test_coordinator.

## Implementation Order

The dev agent MUST follow this order to avoid broken intermediate states:

1. Create `coord_state.py` — move dataclasses/enums, add re-exports to coordinator.py
2. Create `coord_notify.py` — move notify functions, update coordinator.py imports
3. Create `coord_gate.py` — move gate functions, update coordinator.py imports
4. Create `coord_preflight.py` — move preflight functions, update coordinator.py imports
5. Create `coord_workspace.py` — move workspace/merge functions, update imports
6. Run `make fmt && make test` — must pass after each step
7. Split test_coordinator.py — move test classes to new files, update imports
8. Final `make fmt && make test` — must pass

## File Scope

```
src/theforge/coordinator.py
src/theforge/coord_state.py       (new)
src/theforge/coord_notify.py      (new)
src/theforge/coord_gate.py        (new)
src/theforge/coord_preflight.py   (new)
src/theforge/coord_workspace.py   (new)
tests/test_coordinator.py
tests/test_coord_state.py         (new)
tests/test_coord_notify.py        (new)
tests/test_coord_workspace.py     (new)
tests/test_coord_gate.py          (new)
tests/test_coord_preflight.py     (new)
```
