# Coordinator Refactor — Planning Reference

Generated 2026-03-15. Use this as a starting point for the offline Opus planning session.

## Context

- `src/theforge/coordinator.py` — 2,913 lines, 48 functions
- `tests/test_coordinator.py` — 7,518 lines
- Every forge sprint touching coordinator produces merge conflicts in the test file
- Pure refactor: zero behavioral change, full backward-compat re-exports

## What Needs Verification Before Executing

### 1. The `_run_shell` Circular Dependency (Gemini flagged this)

Gemini's plan says `_run_shell` must move from `coordinator.py` to `runner.py`
because `coord_gate.py` and `coord_workspace.py` would need it, creating a
circular import. **Verify this is real** before committing to the move:

```bash
grep -n "_run_shell" src/theforge/coordinator.py | head -20
```

Check which functions in the gate/workspace groups call `_run_shell` directly
vs. which functions get passed shell results from the coordinator loop. If
gate/workspace functions only call `_run_shell` internally and don't need it
from coordinator.py, the circular dep is real and `_run_shell` must move.

If `_run_shell` moves to `runner.py`, also check:
- Does runner.py already import from coordinator? (would create new circular dep)
- What does `_run_shell` import? (subprocess, Path — both stdlib, safe)

### 2. Test Class → File Mapping (Gemini guessed, needs verification)

Gemini's proposed mapping (verify each class name against the actual file):

```bash
grep -n "^class " tests/test_coordinator.py
```

Proposed mapping:
| Class | Destination |
|---|---|
| `TestCoordinatorReviewCycleMetadata` | `test_coord_state.py` |
| `TestCoordinatorHumanReview` | `test_coord_notify.py` |
| `TestRemoteHumanReview` | `test_coord_notify.py` |
| `TestNtfyPollReply` | `test_coord_notify.py` |
| `TestNtfyTerminalNotifications` | `test_coord_notify.py` |
| `TestCoordinatorDirtyWorktree` | `test_coord_gate.py` |
| `TestGateOverride` | `test_coord_gate.py` |
| `TestExitCodeGateMode` | `test_coord_gate.py` |
| `TestCoordinatorPreflight` | `test_coord_preflight.py` |
| `TestParsePreflightComplexity` | `test_coord_preflight.py` |
| `TestComplexityAdaptation` | `test_coord_preflight.py` |
| `TestComplexityIntegration` | `test_coord_preflight.py` |
| `TestLargeComplexitySynthesisP1` | `test_coord_preflight.py` |
| `TestHasPersistentP1` | `test_coord_preflight.py` |
| `TestEscalateDevModel` | `test_coord_preflight.py` |
| `TestDevModelEscalationIntegration` | `test_coord_preflight.py` |
| `TestCoordinatorStaleHandoff` | `test_coord_workspace.py` |
| `TestStaleWorktree` | `test_coord_workspace.py` |
| `TestCoordinatorWorkspaceFailure` | `test_coord_workspace.py` |
| `TestCoordinatorAutoMerge` | `test_coord_workspace.py` |
| `TestCoordinatorAutoPush` | `test_coord_workspace.py` |
| `TestConflictResolution` | `test_coord_workspace.py` |
| Everything else | `test_coordinator.py` (stays) |

### 3. Shared Fixtures and Helpers

Check if `test_coordinator.py` has module-level helpers or fixtures used across
multiple test classes. These need to either:
- Move to `conftest.py` (if shared across new test files)
- Be duplicated into each new file (if small)
- Stay in `test_coordinator.py` with imports in new files

```bash
grep -n "^def \|^@pytest.fixture" tests/test_coordinator.py | head -30
```

### 4. Module-Level Constants in coordinator.py

Some functions moved to sub-modules may reference module-level constants
defined in coordinator.py. Check:

```bash
grep -n "^_[A-Z_]*\s*=" src/theforge/coordinator.py
```

Any constant referenced by functions moving to a sub-module must either move
with those functions or be re-exported.

## Target Import Graph (No Circular Deps)

```
coord_state     ← stdlib only
coord_notify    ← coord_state, config, runner
coord_gate      ← coord_state, config, runner (_run_shell)
coord_preflight ← coord_state, config, runner, task
coord_workspace ← coord_state, config, runner, coord_notify
coordinator     ← all of the above + task, review, schemas
```

## Backward-Compat Re-exports Required

These imports must continue working from `theforge.coordinator`:

```python
# Used by cli.py and sprint.py
from theforge.coordinator import (
    CoordinatorResult,   # → coord_state
    CoordinatorState,    # → coord_state
    Phase,               # → coord_state
    ReviewCycleMetadata, # → coord_state
    _fmt_duration,       # stays in coordinator
    _is_remote_mode,     # → coord_notify
    _notify,             # → coord_notify
    _ntfy_publish,       # → coord_notify
    _run_gate,           # → coord_gate
    generate_audit_log,  # stays in coordinator
    run_from_dev,        # stays in coordinator
    run_from_review,     # stays in coordinator
    run_task,            # stays in coordinator
    set_log_level,       # stays in coordinator
)
```

## Ordered Execution Steps

Each step must pass `make fmt && make test` before proceeding to the next.
Do NOT batch steps.

1. `coord_state.py` — move Phase, ReviewCycleMetadata, CoordinatorState, CoordinatorResult
2. `coord_notify.py` — move all notification functions
3. `coord_gate.py` — move gate + dirty-worktree functions (+ resolve _run_shell dep first)
4. `coord_preflight.py` — move preflight parsing, complexity, escalation
5. `coord_workspace.py` — move worktree lifecycle + merge machinery
6. Slim down `coordinator.py` — remove moved code, add re-exports, verify <800 lines
7. Split `test_coordinator.py` — move test classes per mapping above
8. Final `make fmt && make test` — verify all pass, same count

## Acceptance Criteria

1. `make test` passes with same test count before and after
2. `make lint` passes
3. All re-exports above work without error
4. `coordinator.py` < 800 lines
5. `test_coordinator.py` < 2,000 lines
6. Each new `test_coord_*.py` runs independently: `pytest tests/test_coord_gate.py -v`

## Gemini Plan Output (Raw, 2026-03-15)

Gemini 2.5 Pro produced this plan in 73s without reading the code (based on
spec + function listing only). Use as structural reference but verify all
claims against actual source before trusting.

Stored at: `/tmp/gemini-plan-output.txt` (session-local, may not persist)
