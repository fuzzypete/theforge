---
name: "Sprint parallel mode silently skips dependent stories without auto_merge"
slug: sprint-parallel-dep-safety
pytest_target: tests/test_sprint_runner.py
---

# Sprint parallel mode silently skips dependent stories without auto_merge

## Problem

When a sprint runs with `max_parallel > 1` and stories have `depends_on`
relationships, dependent stories are silently skipped if `auto_merge: true`
is not set. The DAG dependency is never satisfied because parallel mode only
flushes pending merges when `auto_merge` is enabled. The sprint completes
with `specs_skipped: N` and no indication of why.

The user has no way to know this is happening until they read the audit and
investigate the runner source. Money is spent on the first batch of stories,
then the dependent stories are silently dropped.

## Expected behavior

Parallel mode and dependency tracking should compose safely. Either:

- If `max_parallel > 1` and any story in the sprint has `depends_on`,
  `auto_merge: true` is required — the sprint refuses to start and tells
  the user why, or auto-enables it with a warning
- Or, more ambitiously: parallel mode figures out which stories are safe
  to run concurrently based on the dependency graph, and enforces
  `auto_merge` automatically when dependencies are present — the user
  should not need to know that these two settings are coupled

In no case should dependent stories be silently skipped.

## Notes

The root cause is that `effective_am` is forced to `False` in parallel mode
regardless of `manifest.auto_merge`, and the pending-merge flush only runs
when both `max_parallel > 1` AND `auto_merge` are true. The two settings are
coupled but the coupling is invisible.

The longer-term design direction: parallel mode should derive safe concurrency
from the DAG automatically rather than requiring the user to configure it
correctly. Stories with no unmet deps run in parallel; stories with deps wait
for their dependencies to merge before starting. `auto_merge` in this world
becomes a consequence of having deps, not a separate opt-in.
