---
name: "Auto-merge after approved review"
slug: auto-merge
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Auto-Merge

## Problem

After `forge run` completes with DONE (review APPROVE), the branch still
requires manual merging. This is fine for single specs but blocks autonomous
campaign execution where multiple specs run sequentially.

## Context

The coordinator already knows the workspace path, branch name, and base
branch. After a successful review, all that remains is a fast-forward or
squash merge to the base branch and worktree cleanup.

Auto-merge is a *separate concern* from `--interactive` / `--auto`. The
merge decision is orthogonal to the review decision:

- `forge run specs/foo.md` → non-interactive, no auto-merge (current default)
- `forge run specs/foo.md --interactive` → human reviews, no auto-merge
- `forge run specs/foo.md --auto-merge` → non-interactive, merges after APPROVE
- `forge run specs/foo.md --interactive --auto-merge` → human reviews, merges after approve

## Requirements

### 1. Add `--auto-merge` flag to `forge run`

A new CLI flag that, when set, merges the feature branch into the base branch
after the coordinator reaches DONE with review APPROVE (or human APPROVE in
interactive mode).

- Flag name: `--auto-merge`
- Default: `False`
- Must NOT be implied by any other flag

### 2. Merge implementation in coordinator

Add a `_merge_branch()` helper that:

1. `git checkout {base_branch}` in the project root (not worktree)
2. `git merge --ff-only {branch_name}` (fast-forward only — no merge commits)
3. If ff-only fails, fall back to `git merge --no-edit {branch_name}`
4. Return (success, output)

The merge runs in the **project root**, not the worktree. This is because
the worktree IS the branch — you merge the branch into main from main's
working directory.

### 3. Post-merge worktree cleanup

After a successful merge, optionally remove the worktree:

```bash
git worktree remove .forge/worktrees/{slug}
```

If removal fails (e.g., uncommitted files), log a warning but don't fail
the overall run. The worktree is now stale but harmless.

### 4. Coordinator integration

In `run_task()`, after reaching `Phase.DONE`:

- If `auto_merge=True`, call `_merge_branch()`
- If merge succeeds, add `merge.merged: true` to the result
- If merge fails, the run is still SUCCESS (review passed), but
  `merge.error` captures why the merge failed
- The `CoordinatorResult.message` should mention the merge outcome

### 5. Add merge info to audit log

```yaml
merge:
  attempted: true
  merged: true
  base_branch: main
  error: null
```

### 6. Safety checks before merge

Before attempting merge:

- Verify the base branch exists
- Verify no uncommitted changes in the project root
- Verify the branch has diverged from base (there's something to merge)
- If any check fails, skip merge and log the reason

## Acceptance Criteria

- [ ] `forge run specs/foo.md --auto-merge` merges after APPROVE
- [ ] `--auto-merge` without APPROVE does NOT merge (ESCALATE, ALREADY_DONE, BLOCKED)
- [ ] Fast-forward merge is preferred; non-ff is fallback
- [ ] Worktree cleanup attempted after successful merge
- [ ] Merge outcome recorded in audit log
- [ ] `auto_merge` parameter added to `run_task()` and `CoordinatorResult`
- [ ] All existing tests continue to pass
- [ ] New tests cover merge success, merge failure, and safety check scenarios
