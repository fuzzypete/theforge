# Epic: Coordinator Refactor — Modular Architecture

## Vision

`coordinator.py` (2,900+ lines, 48 functions) and `test_coordinator.py`
(7,500+ lines) are the primary bottleneck for forge self-improvement.
Every sprint that touches coordinator logic produces merge conflicts in
the monolithic test file. Split into focused modules with independent
test files so parallel specs don't collide.

## Stories

### Phase 1: Module split
- [ ] `coordinator-refactor.md` — split coordinator.py into 5 sub-modules
      (coord_state, coord_notify, coord_gate, coord_preflight, coord_workspace),
      split test_coordinator.py accordingly, backward-compat re-exports

### Phase 2: Cleanup
- [ ] Remove re-exports once all callers are updated to import from sub-modules
- [ ] Move `_run_shell` and logging helpers to a shared `coord_util.py`

## Definition of Done

- coordinator.py under 800 lines
- test_coordinator.py under 2,000 lines
- Each `test_coord_*.py` independently runnable
- Zero merge conflicts from parallel specs touching different coordinator areas
