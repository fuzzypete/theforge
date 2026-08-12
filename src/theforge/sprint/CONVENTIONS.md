# Sprint subsystem guidance

## Purpose

The sprint subsystem manages multi-story execution concerns: sprint manifests,
issue sourcing, DAG scheduling, launch safety, collision avoidance, CI checks,
and sprint-level audit/display behavior.

## Invariants

- Sprint orchestration must preserve dependency order and launch safety; do not
  bypass DAG, collision, or guard checks for convenience.
- Locking and concurrency behavior must remain deterministic and testable.
  Changes here can create deadlocks or duplicate work if safety checks are
  weakened.
- Sprint logic should coordinate stories and repositories, not absorb unrelated
  coordinator or CLI responsibilities.
- CI and preflight checks should fail closed when required information is
  missing or inconsistent.
- Be careful with lock-related tests: avoid patterns that combine real
  `fcntl.flock` usage with threaded tests under xdist.
- Sprint execution state has one home: `SprintExecutionState` in `runner.py`.
  What a sprint *mutates* lives there; what it merely *consults* lives on the
  frozen `SprintRunContext` it holds. New sprint-wide state goes on one of the
  two — never back into `run_sprint`'s frame, and never into a `nonlocal`.
- Cost and the stop condition each have exactly one owner. Spend is advanced
  only through `SprintExecutionState.cost` (`SprintCostLedger`), and a sprint
  is stopped only through `SprintExecutionState.stop` (`SprintStopCondition`,
  which records the reason and any halt slug together). Do not reintroduce a
  shared total or a bare `stopped_reason` variable alongside them.
- Extracting anything out of `run_sprint` is a move, not a re-decision: the new
  home takes `SprintRunContext` and/or `SprintExecutionState` rather than a
  fresh set of threaded parameters. Passing *members* of the state is the same
  parameter threading under another name and does not count as an extraction.
- `run_sprint` takes the run context and nothing else; build one with
  `SprintRunContext.for_sprint`, which is also where a manifest path is
  resolved. A new sprint-wide input becomes a context field, not a parameter.
- `tests/test_sprint_runner_structure.py` asserts both of the above against the
  parsed module, so re-introducing frame capture fails a test rather than
  passing review.

## Context

- `dag.py` models dependency ordering and is central to launch sequencing.
- `runner.py` coordinates sprint execution across stories once scheduling is
  resolved.
- `manifest.py`, `sources.py`, and `query.py` define how sprint work is
  discovered and represented.
- `launch_guard.py`, `collision.py`, `lock.py`, and `ci_checks.py` are the main
  safety rails for concurrent or repeated execution.
- `display.py` and `audit.py` shape operator-facing visibility into sprint
  progress and outcomes.
