---
name: "Fix stderr tee race in parallel sprint mode"
slug: stderr-tee-fix
pytest_target: tests/
---

# Fix Stderr Tee Race

## Problem

In parallel sprint mode, each story's run_task replaces sys.stderr with a
_TeeStderr that copies output to a per-story log file. sys.stderr is process-global
so story B's tee wraps story A's tee. All output from all threads ends up in
all log files, making parallel logs unreadable.

## Solution

Skip _begin_run_log_tee / _end_run_log_tee when running in a worker thread.
The sprint-level code can manage a single process-level tee, and per-story
logs are written directly via the log_dir mechanism.

## Acceptance criteria

- Parallel sprint: each story's log file contains only that story's output
- Sequential sprint: behavior unchanged (tee still works)
- Per-story log dir (.forge/logs/<slug>/) still captures story output
- All existing tests pass
