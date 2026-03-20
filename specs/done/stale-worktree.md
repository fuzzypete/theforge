---
name: "Worktree resume: detect partial progress and resume from correct phase"
slug: stale-worktree
pytest_target: tests/
---

# Worktree resume

When a forge run is interrupted or escalates, the worktree and branch
stay behind. On the next run for the same spec, the coordinator reuses
the worktree — but it doesn't know where the previous run stopped.

Two failure modes observed in production:

1. **False ALREADY_DONE**: Preflight sees committed code and declares
   all ACs satisfied, skipping review entirely. The code was never
   reviewed or approved. This just happened with `story-format-guidance` —
   dev landed 2 commits, review crashed (Gemini parse failures), process
   died, re-run declared ALREADY_DONE and skipped to DONE.

2. **Nuked work**: `forge run` (not sprint) detected a worktree as stale
   and deleted it, destroying 4 good commits. This happened with
   `set-focus-superset` in HDP.

The coordinator needs to check what phase actually completed before
deciding what to do with an existing worktree.

## Resume logic

When a worktree exists with commits ahead of base:

1. Check if a review APPROVE exists in the audit trail for this slug
2. If yes → ALREADY_DONE is valid (code was reviewed and approved)
3. If no → resume from REVIEW (dev work exists, needs review)
4. If no commits ahead → stale, remove and start fresh

A worktree is stale only when it has zero commits ahead of base OR
the branch is gone. Age alone is not a reason to delete work.

## Acceptance criteria

- Existing worktree with commits but no review APPROVE resumes from REVIEW
- Existing worktree with commits and a prior APPROVE allows ALREADY_DONE
- Existing worktree with zero commits ahead is removed and recreated
- `forge run` supports `--resume` flag (parity with `forge sprint --resume`)
- Clear log output explains resume decision and which phase is resuming
- Never deletes a worktree that has commits ahead of base without `--force`
