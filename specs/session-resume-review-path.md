---
name: "Session resume in forge review path"
slug: session-resume-review-path
pytest_target: tests/
---

# Session Resume in forge review Path

## Problem

Session resume carries dev context across review cycles within a single `forge run`.
But `forge review` — used by sprint resume and standalone review invocations —
initialises a fresh state every time. Each dev iteration inside `forge review`
starts cold, with no memory of what was changed in the previous iteration or why.

This is why convergence degrades across forge review invocations: dev re-reads
everything from scratch, makes shallow fixes, and the reviewer keeps finding
variations of the same problems.

## Requirements

1. Session IDs from a prior dev iteration persist in the worktree between
   invocations
2. When `forge review` runs on an existing worktree, it restores session IDs
   from the prior run before the first dev iteration
3. If no prior session exists, behaviour is unchanged
4. Reviewer session IDs are also persisted and restored

## Acceptance Criteria

- [ ] After a dev iteration with a session ID, the session ID is persisted to
      the worktree
- [ ] A subsequent `forge review` on the same worktree restores the session ID
      before the first dev agent call
- [ ] Reviewer session IDs are also persisted and restored
- [ ] Missing session file → no error, fresh session as before
- [ ] Session file is not committed to git
- [ ] All existing tests pass
