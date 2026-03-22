---
name: "Fix parallel sprint runner — wait for all futures before exit"
slug: parallel-sprint-runner-fix
pytest_target: tests/
---

# Fix Parallel Sprint Runner

## Problem

Every parallel sprint (max_parallel > 1) exits with `outcome: partial` and
`total_cost_usd: 0.0`. The ThreadPoolExecutor workers run but the sprint main
loop returns before collecting any results. Stories progress through all phases
but the sprint never records their outcomes.

Reproduced on 5+ consecutive parallel runs. Sequential (max_parallel=1) works.

## Root cause

The `while not dag.is_done()` loop in `run_sprint()` submits all tasks and
calls `wait(FIRST_COMPLETED)`, but exits the loop before any future completes.
The exact mechanism needs investigation — likely the loop condition, the
`if not active` deadlock break, or an interaction between dag state and the
wait call.

## Acceptance criteria

- Parallel sprint with 3 independent stories runs to completion
- Sprint summary records all story outcomes with correct costs
- `outcome` is `done` (not `partial`) when all stories succeed
- `wait()` blocks until at least one future completes per iteration
- Stories that fail don't prevent other stories from completing
- Sequential mode (max_parallel=1) is unaffected
- All existing tests pass
- New test: 3-story parallel sprint with mocked run_task completes correctly
