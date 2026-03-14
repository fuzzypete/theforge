---
name: "Stale worktree detection: prevent ALREADY_DONE false positives"
slug: stale-worktree
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Stale Worktree Detection

## Problem

When a forge run escalates or is interrupted, the worktree and branch are
left behind. On the next run for the same spec, the coordinator reuses the
existing worktree. PREFLIGHT reads the partially-implemented code and
reports ALREADY_DONE — even when spec-required tests are missing or the
implementation is broken.

Real occurrences: `audit-improvements`, `human-in-the-loop`,
`gate-hardening`, and `reasoning-effort` all false-ALREADY_DONE'd across
multiple sprints because leftover worktrees from escalated prior runs
contained partial code.

## Root Cause

`_setup_workspace()` in coordinator.py runs `git worktree add` to create
the workspace. If the worktree directory already exists, the command is
skipped (or fails silently). PREFLIGHT then runs against stale code on a
branch that never merged to main, has no relationship to the current spec
state, and may be partially or incorrectly implemented.

## Fix

Before creating the workspace, check whether a worktree already exists
for this slug. If it does, determine whether it is **safe to reuse**:

A worktree is safe to reuse if and only if:
- The branch has commits ahead of the base branch (work in progress)
- AND the last commit is recent (within `stale_worktree_days`, default 1)

Otherwise, treat it as stale: remove the worktree and delete the branch,
then proceed with a fresh `git worktree add`.

## Requirements

### R1: Staleness check in `_setup_workspace()`

Before running `workspace.create_command`, check if the worktree path
already exists:

```python
worktree_path = config.workspace.path_pattern.format(slug=task.slug)
if Path(worktree_path).exists():
    if _is_stale_worktree(worktree_path, base_branch, config):
        _remove_worktree(worktree_path, branch_name)
    # else: reuse — fall through to create_command (will be a no-op or error)
```

### R2: `_is_stale_worktree(path, base_branch, config) -> bool`

Returns `True` (stale, should be removed) when ANY of:

1. **No commits ahead of base**: `git log base..branch --oneline` returns
   empty — branch has no new work, nothing to preserve
2. **Last commit is old**: last commit timestamp is older than
   `config.workspace.stale_worktree_days` (default: 1 day)
3. **Branch does not exist**: worktree dir exists but the branch is gone
   (corrupted state from prior interrupted cleanup)

Returns `False` (safe to reuse) only when branch has commits ahead of base
AND those commits are recent.

### R3: `_remove_worktree(path, branch) -> None`

```python
subprocess.run(["git", "worktree", "remove", "--force", path], ...)
subprocess.run(["git", "branch", "-D", branch], ...)
```

Log a clear warning before removing:
```
[forge] ⚠ WORKSPACE  stale worktree detected — removing feat/<slug>
[forge]   Branch had 0 commits ahead of main (last run escalated or was interrupted)
```

Both commands are best-effort — log failures but don't raise. The
subsequent `git worktree add` will fail clearly if cleanup was incomplete.

### R4: `stale_worktree_days` config

Add to `WorkspaceConfig`:

```python
stale_worktree_days: int = 1
```

Parse from `forge.yaml`:
```yaml
workspace:
  stale_worktree_days: 1   # remove worktrees older than N days; 0 = always remove
```

`stale_worktree_days: 0` means always remove any existing worktree (useful
for CI or automated sprints where you always want a clean slate).

### R5: Log the decision

Always log what was found and what was decided:

```
[forge] ⚠ WORKSPACE  existing worktree found: .forge/worktrees/audit-improvements
[forge]   0 commits ahead of main, last commit 3 days ago — removing (stale)
[forge]   Removed stale worktree and branch feat/audit-improvements
```

Or if reusing:
```
[forge] ↻ WORKSPACE  reusing existing worktree: .forge/worktrees/my-spec
[forge]   3 commits ahead of main, last commit 12 minutes ago
```

### R6: Tests

Add to `tests/test_coordinator.py` — `TestStaleWorktree` class:

- `test_no_existing_worktree`: path doesn't exist → `_is_stale_worktree`
  not called, normal workspace creation proceeds
- `test_stale_zero_commits_ahead`: branch has 0 commits ahead of base →
  `_is_stale_worktree` returns True, `_remove_worktree` called
- `test_stale_old_commit`: branch has commits but last commit is >1 day
  old → stale, removed
- `test_fresh_worktree_reused`: branch has commits ahead, last commit
  recent → `_is_stale_worktree` returns False, `_remove_worktree` not called
- `test_stale_worktree_days_zero_always_removes`: `stale_worktree_days=0`
  → always removes even with recent commits
- `test_remove_worktree_logs_warning`: verify warning is logged before removal
- `test_remove_failure_does_not_raise`: `git worktree remove` fails →
  logged, does not raise

## Out of Scope

- Automatically resuming work from a recent non-stale worktree (that is
  a separate "resume" feature)
- Prompting the human before removing (silent removal is correct for
  unattended sprint runs)
- Cleaning up ALL stale worktrees at once (`forge clean` command — separate spec)

## Acceptance Criteria

- [ ] Escalated or interrupted runs no longer cause ALREADY_DONE false positives
      on the next run for the same spec
- [ ] A stale worktree (0 commits ahead or old last commit) is automatically
      removed and recreated before PREFLIGHT runs
- [ ] A fresh in-progress worktree (recent commits ahead of main) is reused
- [ ] `stale_worktree_days: 0` always removes existing worktrees
- [ ] Clear log output explains the staleness decision
- [ ] All existing tests pass
- [ ] New tests cover stale/fresh/zero-days detection and removal
