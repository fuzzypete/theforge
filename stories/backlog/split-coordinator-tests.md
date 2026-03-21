---
name: Split coordinator test file
slug: split-coordinator-tests
depends_on:
  - extract-coord-notify
  - extract-coord-gate
  - extract-coord-preflight
  - extract-coord-workspace
---

# Spec: Split Coordinator Test File

## Goal

Split `tests/test_coordinator.py` (~7,500 lines) into focused test files,
one per sub-module. Zero behavioral change — same tests, new file locations.

## Target Structure

| New file | Test classes to move |
|---|---|
| `tests/test_coord_state.py` | TestCoordinatorState, TestCoordinatorResult, TestPhase |
| `tests/test_coord_notify.py` | TestNtfy*, TestHumanReview, TestRemoteHumanReview, TestNotify |
| `tests/test_coord_gate.py` | TestRunGate*, TestDirtyWorktree, TestAutoCommit, TestParseFiles |
| `tests/test_coord_preflight.py` | TestPreflight*, TestComplexity*, TestEscalate*, TestPersistentP1 |
| `tests/test_coord_workspace.py` | TestWorkspace*, TestMerge*, TestStaleWorktree |
| `tests/test_coordinator.py` | Everything else (coordinator loop, run_task, audit log) |

## Rules

- Each new file imports from the sub-module directly (`from theforge.coord_gate import ...`)
  AND via coordinator re-exports (`from theforge.coordinator import ...`) — both must work.
- Each new file must be independently runnable: `pytest tests/test_coord_gate.py -v` works alone.
- Update `tests/conftest.py` to patch both `theforge.coordinator.X` and `theforge.coord_notify.X`
  for notification functions.

## Acceptance Criteria

1. `make test` passes with same test count.
2. `test_coordinator.py` is under 2,000 lines.
3. Each `tests/test_coord_*.py` runs independently with `pytest tests/test_coord_X.py -v`.
4. `make lint` passes.
