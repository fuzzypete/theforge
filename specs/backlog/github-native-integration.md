---
name: "Native GitHub integration — first-class PR and issue support"
slug: github-native-integration
pytest_target: tests/
---

# Native GitHub Integration

## Problem

GitHub support is currently split across three mechanisms with no coherent
ownership:

- PR creation: `on_approve: pr` in forge.yaml → coordinator calls `gh pr create`
- Issue filing: `post_run.sh` hook → shell script calls `gh api`
- Reviewer attribution: `FORGE_GH_PR_REVIEWS` env var → same shell script

This means a GitHub-integrated project needs: forge.yaml config, a shell hook
script, AND an env var. The hook is an escape hatch (correct for general use),
but for GH it's just boilerplate scaffolding around things forge should own
directly.

## Solution

Add a `github` section to forge.yaml as a first-class integration block. When
`github.enabled: true`, the coordinator and post-run logic own GH behaviors
directly — no hook script required. Hooks remain available for custom or
non-GH CI behavior on top.

## forge.yaml config

```yaml
github:
  enabled: true
  on_approve: create_pr       # create PR on APPROVE (replaces on_approve: pr)
  on_finding: file_issue      # P1 findings → GH issues automatically
  pr_reviews: true            # post per-reviewer review comments on PR
```

All keys optional with sensible defaults when `enabled: true`:
- `on_approve` defaults to `create_pr`
- `on_finding` defaults to `file_issue`
- `pr_reviews` defaults to `true`

## Behavior

### on_approve: create_pr

Same as current `on_approve: pr` — coordinator calls `gh pr create` after a
review APPROVE. Migrate existing config key; keep backward compat with
`on_approve: pr` as an alias during transition.

### on_finding: file_issue

After each review cycle, P1 findings are automatically filed as GH issues with
`forge-finding` + `p1` labels. Currently done in `post_run.sh`. Move into
coordinator post-run logic, reading from the synthesized review result.

P2 findings filed with `forge-finding` + `p2` labels.

P2 findings on APPROVE are especially important — these are issues the
reviewers flagged but didn't block on. Without issue filing they vanish
into the audit log.

Deduplication: check open issues for matching title before filing (avoid
re-filing the same finding across review cycles).

Labels: `forge-finding` + `p1`/`p2` + `code-review`. Plan review P2s → GH
issues is an **open question** — may depend on whether code review catches
them downstream.

### pr_reviews: true

After APPROVE + PR creation, post per-reviewer COMMENT reviews and a final
APPROVE review via `gh api`. Currently done in `post_run.sh` behind
`FORGE_GH_PR_REVIEWS` env var. Move into coordinator/post-run Python logic.

## Migration

- `on_approve: pr` in existing forge.yaml → still works (alias)
- `FORGE_GH_PR_REVIEWS` env var → ignored when `github.enabled: true`
- `post_run.sh` GH blocks → no longer scaffolded by `forge init-hooks` when
  `github.enabled: true`; hook still scaffolded for custom behavior

## Acceptance Criteria

- [ ] `github` block parsed in `config.py` with `enabled`, `on_approve`,
      `on_finding`, `pr_reviews` fields
- [ ] `on_approve: pr` still works as alias for backward compat
- [ ] When `github.enabled: true`, coordinator creates PR on APPROVE without
      needing `post_run.sh`
- [ ] When `github.enabled: true` and `on_finding: file_issue`, P1 findings
      filed as GH issues on every review cycle
- [ ] P2 findings on APPROVE filed as GH issues with `p2` + `code-review` labels
- [ ] Finding deduplication: no duplicate issues filed for the same finding
- [ ] When `github.enabled: true` and `pr_reviews: true`, per-reviewer reviews
      posted on PR after APPROVE
- [ ] `FORGE_GH_PR_REVIEWS` env var no longer required
- [ ] `forge init-hooks` does not scaffold GH blocks in hook when
      `github.enabled: true`
- [ ] PR body includes `Closes #N` for matching milestone issue (auto-close
      on merge). Match by slug against open issues or via `gh_issue` frontmatter
      field in spec file.
- [ ] `forge.yaml` in this project updated to use `github:` block
- [ ] All existing tests pass; new tests for config parsing and GH integration
      logic
