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
- A responsibility moved out of `runner.py` does not import it back.
  `audit_publish.py` is the first move (#2402) and the shape to copy: it takes
  the execution state (and reads the run context off it) rather than a list of
  members, and it depends on the writers in `audit.py`, never on the runner.
  `tests/test_sprint_audit_publish.py` asserts the absence of that import, so a
  relocation-with-a-dependency-left-behind fails a test. The cost of annotating
  the state without importing the runner is deliberate: the parameter is `Any`
  and documented, because the alternative is exactly the coupling the move
  removed.

## Context

- `dag.py` models dependency ordering and is central to launch sequencing.
- `runner.py` coordinates sprint execution across stories once scheduling is
  resolved.
- `manifest.py`, `sources.py`, and `query.py` define how sprint work is
  discovered and represented.
- `launch_guard.py`, `collision.py`, `lock.py`, and `ci_checks.py` are the main
  safety rails for concurrent or repeated execution.
- `live_stories.py` and `story_executions.py` answer one question between them:
  is this worktree the asking run's own in-flight work, or someone else's?
  `story_executions.py` holds the ownership records the scheduler writes before
  dispatch and clears only after settling a story; `live_stories.py` folds those
  together with surviving agent process groups into the liveness resolution the
  launch guard consults. Contention detection is answered by establishing whose
  work a resource is — a resource owned by the asking run is never contention,
  and a run that cannot prove a resource foreign does not treat it as foreign.
- `display.py` and `audit.py` shape operator-facing visibility into sprint
  progress and outcomes.
- `audit_publish.py` owns the end of a run: it builds the terminal audit and
  summary inputs from the run's state, writes the audit/summary/RCA artifacts,
  and commits and pushes the canonical per-run audit JSON to the base branch.
