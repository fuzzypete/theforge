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
