---
name: "Commit dirty worktree state before merge on approve"
slug: commit-dirty-worktree-on-approve
pytest_target: tests/
---

# Commit Dirty Worktree State on Approve

## Problem

When a story reaches DONE and `_finalize_approve` fires, it proceeds directly to
`_merge_branch` without checking for uncommitted files in the worktree. Any files
the dev agent created but didn't `git add` — handoff artifacts, generated configs,
test fixtures, etc. — survive as dirty state.

The only auto-commit today is `_auto_commit_side_effects` in the VALIDATE phase,
which handles fmt reformats. Nothing catches general dirty state at approval time.

This means:
- Worktrees accumulate uncommitted files across dev/review cycles
- If approval never fires (e.g., false P1 blocks it), the worktree is left with
  orphaned uncommitted work
- If approval does fire and merges, the uncommitted files are silently lost — they
  exist in the worktree but not in the merged branch

## Solution

Add a dirty-worktree check in `_finalize_approve`, before `_merge_branch`:

1. Run `git status --porcelain` in the worktree
2. If there are tracked-but-uncommitted changes (modified/added/deleted, not
   untracked `??`), stage and commit them with a message like
   `"chore: commit remaining worktree changes before merge"`
3. If there are untracked files, log a warning listing them but do not auto-add
   (untracked files may be intentionally excluded — `.gitignore` should govern)
4. Proceed to merge as normal

Reuse the existing `_parse_dirty_files` helper in `gate.py` which already parses
`git status --porcelain` and skips untracked/ignored entries.

## Acceptance criteria

- Tracked modified/added/deleted files are committed before merge in
  `_finalize_approve`
- Untracked files are logged as warnings but not auto-added
- The commit message clearly identifies these as pre-merge cleanup
- If `git status` is clean, no commit is created (no empty commits)
- Existing auto-commit of fmt side-effects in VALIDATE is unchanged
- Tests cover: dirty worktree committed, clean worktree no-op, untracked files
  warned but not added
