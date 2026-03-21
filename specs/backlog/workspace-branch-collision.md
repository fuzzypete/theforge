---
name: "WORKSPACE handles existing branches gracefully"
slug: workspace-branch-collision
pytest_target: tests/
---

# WORKSPACE Branch Collision

## Problem

When `forge run` is called for a spec whose `feat/<slug>` branch already
exists from a prior run, `git worktree add` fails:

```
fatal: a branch named 'feat/fix-polar-auto-reconnect' already exists
```

The sprint escalates immediately with $0 spent and zero useful work done.
This happens when:

- A prior run was killed or escalated, leaving the branch behind
- The user deleted the worktree directory but not the branch
- `--resume` wasn't used but the branch still exists

## Solution

When `git worktree add -b feat/<slug>` fails because the branch exists,
the WORKSPACE phase should:

1. Check if a worktree is already linked to that branch
   - If yes and directory exists: reuse it (same as `--resume`)
   - If yes but directory is gone: `git worktree prune`, then recreate
2. If no worktree but branch exists:
   - Check if branch has unmerged commits ahead of base
     - If yes: reuse the branch (`git worktree add` without `-b`)
     - If no (0 commits ahead): delete the stale branch, create fresh

This makes `forge run` idempotent — running the same spec twice does the
right thing without requiring `--resume` or manual cleanup.

## Acceptance Criteria

- [ ] `forge run` succeeds when `feat/<slug>` branch already exists
- [ ] Existing worktree with commits: reused (resume behavior)
- [ ] Existing worktree directory missing: pruned and recreated
- [ ] Branch exists with unmerged commits, no worktree: reattach worktree
- [ ] Branch exists with 0 commits ahead: delete branch, create fresh
- [ ] Log messages clearly indicate which path was taken
- [ ] All existing tests pass
- [ ] New tests for each collision scenario
