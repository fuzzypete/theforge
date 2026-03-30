---
name: "WORKSPACE: pull base branch before creating worktree"
slug: workspace-auto-pull
pytest_target: tests/test_coord_workspace.py
---

# WORKSPACE: pull base branch before creating worktree

## Problem

`_create_workspace` branches the new worktree from whatever local `base_branch` is at
that moment. If the remote has new commits (e.g. a PR just merged), the worktree
silently starts from a stale base. The developer only discovers this at review time
when the reviewer flags changes that "already exist on main."

## Expected behavior

- Before creating a **fresh** worktree, the coordinator runs
  `git pull --ff-only origin <base_branch>` in the project root
- If the pull fails (non-fast-forward, offline, no remote), log a warning and
  continue — do not block the run
- On **resume** (reusing an existing worktree), skip the pull but log a one-line
  note if `base_branch` is behind origin (informational only)
- A `--no-pull` CLI flag on `forge run` and `forge sprint run` suppresses the pull
  entirely (for offline or CI use)
