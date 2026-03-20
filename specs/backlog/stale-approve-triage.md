---
name: "Fix stale APPROVE triage false-positive in has_review_approve"
slug: stale-approve-triage
pytest_target: tests/test_sprint.py tests/test_coord_audit.py
---

# Fix stale APPROVE triage false-positive

## Problem

`has_review_approve()` in `coord_audit.py` reads `history.jsonl` and returns
`True` if any prior run for a slug produced a review APPROVE. `_triage_spec()`
in `sprint.py` uses this to mark specs as `skip` — skipping the entire sprint
including PR creation.

The bug: an APPROVE record from an **abandoned run** is treated the same as
one from a completed merged run. If a sprint ran, got an APPROVE, and then was
abandoned (branch still has unmerged commits), the next `--resume` sees the
APPROVE and skips the spec entirely — no PR is ever created, no merge happens,
and the work just sits on the branch forever.

## Fix

In `has_review_approve()`, after finding an APPROVE record for the slug, check
whether the feature branch (`feat/<slug>`) still exists and has unmerged commits
ahead of the base branch. If it does, the APPROVE is from an abandoned run —
skip it and keep searching. Only return `True` if the APPROVE is from a
genuinely completed run (branch merged or branch gone).

Thread `base_branch` through from the `_triage_spec()` call site so the check
uses the correct base (default `"main"`).

Add a helper `_branch_has_unmerged_commits(project_root, branch, base)` that
runs `git rev-list base..branch --count` — consistent with the existing
worktree triage logic in `sprint.py`.

## Acceptance Criteria

- [ ] `has_review_approve()` returns `False` when the APPROVE record exists but
      `feat/<slug>` has unmerged commits ahead of base
- [ ] `has_review_approve()` returns `True` when the APPROVE record exists and
      the branch has been merged (0 commits ahead or branch absent)
- [ ] `_branch_has_unmerged_commits()` handles missing branch (returns `False`),
      subprocess timeout (returns `False`), and non-integer output (returns `False`)
- [ ] `base_branch` parameter threaded through from `_triage_spec()` call site
- [ ] New tests in `test_coord_audit.py` cover: stale APPROVE (branch ahead),
      valid APPROVE (branch merged), valid APPROVE (branch absent), no APPROVE record
- [ ] All existing tests pass
