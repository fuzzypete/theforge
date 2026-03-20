---
name: "PR on approve — create GitHub PR instead of auto-merge"
slug: pr-on-approve
pytest_target: tests/
---

# PR on Approve

## Problem

`--auto-merge` bypasses pull request review entirely. For published projects,
PRs provide trust signals (review history, CI checks, discussion) and a natural
place for human oversight. But requiring manual `gh pr create` after every forge
run defeats the automation purpose.

## Solution

A `pr_on_approve` option in `forge.yaml` that creates a GitHub PR (via `gh`)
when a story reaches APPROVE, instead of merging directly. The PR includes
the review summary, findings, and cost in the body.

## forge.yaml config

```yaml
workspace:
  on_approve: pr          # "merge" (default, current behavior) | "pr" | "none"
  pr_labels:              # optional: labels to apply
    - forge-approved
  pr_draft: false         # optional: create as draft PR
```

`on_approve` controls post-APPROVE behavior:
- `merge` — current `--auto-merge` behavior (merge branch to base)
- `pr` — create a PR via `gh pr create` with structured body
- `none` — do nothing (leave branch for manual handling)

The `--auto-merge` CLI flag continues to work as a shorthand for `on_approve: merge`.
When both are set, CLI flag wins.

## PR body template

```markdown
## Summary

{review_summary}

## Review

- **Verdict:** APPROVE ({p1_count} P1, {p2_count} P2)
- **Reviewers:** {reviewer_names}
- **Cost:** ${total_cost:.2f}
- **Dev iterations:** {dev_iterations}
- **Tests:** {test_count} passed

## Findings

{p2_findings_if_any}

## Spec

{spec_name} (`{spec_path}`)

---
*Created automatically by [TheForge](https://github.com/fuzzypete/theforge)*
```

## Implementation

### `src/theforge/config.py`
- Add `on_approve: str` to `WorkspaceConfig` (values: "merge", "pr", "none"; default: "merge")
- Add `pr_labels: list[str]` and `pr_draft: bool` to `WorkspaceConfig`
- Parse from `forge.yaml` `workspace` block

### `src/theforge/coordinator.py`
- After APPROVE verdict, check `config.workspace.on_approve`:
  - `"merge"` → existing merge logic (unchanged)
  - `"pr"` → call `_create_pr()`
  - `"none"` → skip, log "Branch ready for manual review"
- `_create_pr()`: build PR title + body from state, invoke `gh pr create`
  via subprocess, log PR URL, record in audit
- `--auto-merge` CLI flag overrides `on_approve` to `"merge"`

### `src/theforge/coord_phases.py`
- Extract merge logic into `_handle_post_approve()` that dispatches on
  `on_approve` setting
- PR creation is best-effort: failure logs a warning but doesn't change
  the forge outcome (DONE stays DONE)

### Audit
- `forge_audit.yaml` `merge:` field becomes `post_approve:` with:
  - `action: "pr" | "merge" | "none"`
  - `pr_url:` (when action=pr)
  - `success: bool`
  - `error:` (if failed)

## Acceptance Criteria

- [ ] `forge.yaml` `workspace.on_approve` parsed into config (default: "merge")
- [ ] `on_approve: pr` creates a GitHub PR via `gh pr create` after APPROVE
- [ ] PR body includes review summary, findings, cost, reviewer names
- [ ] `on_approve: none` skips merge and PR, logs branch name
- [ ] `--auto-merge` CLI flag overrides `on_approve` to `"merge"`
- [ ] PR creation failure: warning logged, forge outcome unchanged (DONE)
- [ ] `pr_labels` applied to PR when configured
- [ ] `pr_draft: true` creates draft PR
- [ ] Audit records PR URL or merge result
- [ ] Sprint mode respects `on_approve` per-run
- [ ] All existing tests pass
- [ ] New tests for PR creation path (mock subprocess)
