---
name: "on_approve: merge-pr — auto-merge with PR audit trail"
slug: on-approve-merge-pr
github_issue: 159
pytest_target: tests/
---

# on_approve: merge-pr

## Problem

TheForge treats PR creation and auto-merge as mutually exclusive:
- `on_approve: merge` — raw git merge to main, no PR, no audit trail
- `on_approve: pr` — creates a PR, requires manual merge

These are distinct concerns that should be composable.

The concrete failure case: parallel sprint execution of dependent stories
requires auto-merge so that story B's worktree branches from main after
story A's changes land. Without auto-merge, dependent stories build on stale
code and produce merge conflicts or reintroduce removed code. But raw merge
loses the audit trail — no PR means no diff review surface, no CI check
record, no comment thread.

## Solution

New option `on_approve: merge-pr` that creates a PR and immediately merges it.

### Behavior

1. After story passes review, forge creates a PR from the feature branch to
   `base_branch` using `gh pr create`
2. PR body includes: story name, dev cost, review summary, findings addressed
3. Forge immediately merges the PR via `gh pr merge` (strategy per config)
4. The merged PR persists as a permanent audit record — reviewable after the fact
5. Dependent stories waiting in the sprint can now branch from updated main

### Config surface

```yaml
workspace:
  on_approve: merge-pr     # new option alongside: merge, ask, pr
  merge_strategy: squash   # merge | squash | rebase (default: squash)
  auto_push: true          # required — PR needs a remote branch
```

### Edge cases

- **Merge conflict**: attempt rebase onto latest main before creating the PR;
  if rebase fails, escalate — do not force-push
- **Simultaneous merges in parallel sprint**: queue merges sequentially;
  the sprint loop already has a merge lock — extend it to cover `gh pr merge`
- **`auto_push: false` with `merge-pr`**: invalid combination — warn at
  config load time (or raise `ConfigError` if config-normalization story has
  shipped)

### Not in scope

GitHub branch protection rules or external status check configuration.
`merge-pr` is self-contained — forge owns the full lifecycle without
depending on repo-level GitHub settings. It must work for any repo without
admin configuration.

## Acceptance criteria

- `on_approve: merge-pr` creates a PR and merges it in a single coordinator step
- PR body contains story name, dev cost, and review summary
- `merge_strategy: merge | squash | rebase` respected
- Merge conflict → rebase attempted → escalate on failure (not force-push)
- Parallel sprint merge lock covers `gh pr merge` calls
- `auto_push: false` + `merge-pr` emits a config warning/error
- `on_approve: merge` and `on_approve: pr` behavior unchanged
- All existing tests pass
- New tests for the merge-pr path (mock `gh` calls, conflict path, lock behavior)
