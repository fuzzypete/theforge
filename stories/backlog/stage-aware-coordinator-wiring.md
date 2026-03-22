---
name: "Stage-aware coordinator wiring — connect --from/--until to phase loop"
slug: stage-aware-coordinator-wiring
pytest_target: tests/
---

# Stage-aware coordinator wiring

## Problem

The stage-aware pipeline story (#27) shipped CLI parsing for `--from` and
`--until` flags, added `start_phase`/`stop_phase` to `CoordinatorState` and
`run_task()` signature, and wrote 5 integration tests. However, a merge
conflict during the M3 sprint caused the coordinator phase loop wiring to
be dropped. The flags parse correctly and pass through to `run_task()`, but
the coordinator ignores them — it always runs the full pipeline.

The 5 tests (`TestUntilPhaseStop`, `TestFromPhaseSkip`) are currently
`@pytest.mark.skip(reason="stage-aware coordinator wiring pending")`.

## Acceptance criteria

- `run_task()` respects `start_phase`: phases before `start_phase` are skipped
- `run_task()` respects `stop_phase`: pipeline stops cleanly after `stop_phase`
- Combined `--from` + `--until` works
- Precondition checks: `--from dev` requires plan exists, `--from review`
  requires dev handoff exists
- All 5 skipped tests unskipped and passing
- Audit YAML records start/stop phases when set
- No regressions in existing tests
