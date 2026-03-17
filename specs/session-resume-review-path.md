---
name: "Session resume in forge review path"
slug: session-resume-review-path
pytest_target: tests/
---

# Session Resume in forge review Path

## Problem

`session-resume` wired `dev_session_id` into `run_task()` so that dev
iterations carry full session context across review cycles. But
`run_from_review()` — the entry point used by `forge review` and
`forge sprint --resume` — initialises a fresh `CoordinatorState` with
`dev_session_id = None` every time.

Result: every dev iteration inside `forge review` starts cold. The agent
re-reads every file from scratch, loses all context about *why* it made
previous decisions, and produces shallow fixes — exactly the problem
session-resume was built to solve.

This is why `plan-review` exhausted 3 cycles on a 2-line fix: each repair
iteration had no memory of what the previous iteration changed.

## Root Cause

`_setup_resume_entry()` creates a blank `CoordinatorState`. There is no
mechanism to persist `dev_session_id` (or `reviewer_session_ids`) between
invocations of `forge review`. Each CLI invocation starts fresh.

## Solution

Persist session IDs to the worktree between invocations so `run_from_review()`
can restore them on the next call.

### 1. Write session IDs to worktree after each dev iteration

In `_coordinator_loop`, after a successful dev agent call that sets
`state.dev_session_id`, write a `.forge_sessions.yaml` file to the worktree:

```yaml
dev_session_id: "<session_id>"
reviewer_session_ids:
  codex: "<session_id>"
  gemini: "<session_id>"
```

Only write fields that are non-None. Overwrite on every update.

File location: `<workspace_path>/.forge_sessions.yaml`

Add `.forge_sessions.yaml` to `.gitignore` — this is a runtime artifact,
not part of the implementation.

### 2. Restore session IDs in `_setup_resume_entry()`

After constructing `CoordinatorState`, check for `.forge_sessions.yaml`
in `workspace_path`. If present, load and restore:

```python
sessions_file = workspace_path / ".forge_sessions.yaml"
if sessions_file.exists():
    sessions = yaml.safe_load(sessions_file.read_text())
    if sessions.get("dev_session_id"):
        state.dev_session_id = sessions["dev_session_id"]
    for name, sid in sessions.get("reviewer_session_ids", {}).items():
        state.reviewer_session_ids[name] = sid
    _log_verbose(f"  ↺ Restored session IDs from prior run")
```

### 3. Write sessions in `run_task()` too

`run_task()` uses the same `_coordinator_loop` so the write happens
automatically. No separate change needed — the write in `_coordinator_loop`
covers both paths.

### 4. Gitignore

Add `.forge_sessions.yaml` to `.gitignore` (or `.forge/worktrees/**/.forge_sessions.yaml`).

## Files to Modify

| File | Change |
|------|--------|
| `src/theforge/coordinator.py` | Write `.forge_sessions.yaml` after dev result captures session_id; restore in `_setup_resume_entry()` |
| `.gitignore` | Add `.forge_sessions.yaml` |
| `tests/test_coordinator.py` | New tests (see below) |

## Acceptance Criteria

- [ ] After a dev iteration with a session_id, `.forge_sessions.yaml` is
      written to the worktree
- [ ] `run_from_review()` restores `dev_session_id` from `.forge_sessions.yaml`
      if present
- [ ] `run_from_review()` restores `reviewer_session_ids` from
      `.forge_sessions.yaml` if present
- [ ] If `.forge_sessions.yaml` is absent, behaviour is unchanged (no error)
- [ ] `.forge_sessions.yaml` is in `.gitignore`
- [ ] Session IDs are overwritten (not appended) on each update
- [ ] Verbose log line emitted when sessions are restored
- [ ] All existing tests pass

## New Tests

1. **test_sessions_written_after_dev** — after a dev iteration that returns
   a session_id, verify `.forge_sessions.yaml` exists in the worktree with
   the correct `dev_session_id`.

2. **test_sessions_restored_in_run_from_review** — write a
   `.forge_sessions.yaml` to a tmp worktree, call `run_from_review()`,
   verify `state.dev_session_id` is set before the first dev agent call.

3. **test_sessions_absent_no_error** — call `run_from_review()` with no
   `.forge_sessions.yaml` present, verify it runs without error and
   `state.dev_session_id` is None.

4. **test_sessions_overwritten_on_update** — two dev iterations with
   different session_ids, verify the file contains the second (latest) id.

5. **test_reviewer_sessions_restored** — write reviewer session ids to
   `.forge_sessions.yaml`, verify they are passed to `run_agent_pool` on
   the next review cycle.
