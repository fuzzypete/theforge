---
name: "Plan review pool — multi-model plan review"
slug: plan-review-pool
pytest_target: tests/
---

# Plan Review Pool

## Problem

`plan_agent_review` currently supports a single reviewer. Code review already
supports a pool of reviewers with deterministic verdict merging. Plan review —
the gate that catches bad plans before burning dev iterations — deserves the
same multi-model treatment.

The coordinator already has the merging infrastructure (`merge_review_results`
for code review, `PlanReviewResult` for plan review). The gap is that
`plan_agent_review` in config accepts a single profile, not a list, and the
coordinator calls `run_agent()` once instead of running a pool.

## Goal

`plan_agent_review` accepts either a single profile (backward compatible) or a
pool of reviewers. When multiple reviewers are configured, they run in parallel
and their verdicts are merged deterministically — same pattern as code review.

## Config

### Single reviewer (current, still works):

```yaml
plan_agent_review:
  enabled: true
  cli: claude
  model: opus
  budget_usd: 2.00
  timeout: 600
```

### Pool (new):

```yaml
plan_agent_review:
  enabled: true
  pool:
    - name: opus-plan-reviewer
      cli: claude
      model: opus
      budget_usd: 2.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]
    - name: codex-plan-reviewer
      provider: openai
      model: gpt-5.1-codex-mini
      budget_usd: 1.00
      timeout_seconds: 120
      allowed_tools: [Read, Glob, Grep]
```

When `pool` is present, the top-level cli/model/budget/timeout fields are
ignored. When `pool` is absent, the top-level fields define a single-reviewer
pool of size 1 (backward compatible).

## Acceptance Criteria

### AC-1: Config parsing

- `PlanAgentReviewConfig` gains a `pool: list[ModelProfile]` field
- When `pool` key is present in YAML, parse each entry as a `ModelProfile`
  using `_parse_profile()` (same as code review pool)
- When `pool` key is absent, construct a single-element pool from the existing
  top-level fields (cli, model, budget_usd, timeout)
- Validation: pool must be non-empty when enabled

### AC-2: Parallel plan review execution

- The coordinator runs all plan reviewers in parallel using `run_agent_pool()`
  (for CLI profiles) and `run_api_agent()` (for API profiles), same dispatch
  as code review
- Each reviewer gets the same plan review prompt
- Results are collected and failures (exit != 0) are logged but excluded from
  merging, same as code review

### AC-3: Verdict merging

- Plan review verdicts are merged deterministically:
  - Any P0 finding from any reviewer → REJECT (blocks plan)
  - All APPROVE with only P1 findings → advisory downgrade (existing behavior),
    findings passed to dev
  - All APPROVE with no findings → APPROVE
- Parse errors from individual reviewers do not auto-reject — only reviewers
  that produced parseable output participate in the merge
- If all reviewers fail to parse, the merged result is REJECT with parse errors

### AC-4: Session resume

- Each plan reviewer in the pool gets its own session ID tracking, same pattern
  as code review pool (`reviewer_session_ids` dict)
- `plan_review_session_id` (singular) is deprecated in favor of
  `plan_review_session_ids` (dict keyed by reviewer name)

### AC-5: Logging

- Log format matches code review pool:
  ```
  [forge] ▸ PLAN_REVIEW   opus+codex-mini  cycle=1
  [forge] Running 2 plan reviewer(s): ['opus-plan-reviewer', 'codex-plan-reviewer']
  ```
- Individual reviewer results logged with name, status, cost

### AC-6: Audit trail

- Plan review audit entry includes per-reviewer results (same structure as
  code review audit)

### AC-7: Tests

- `test_config.py`: pool config parsed correctly, backward compat with single
  reviewer config
- `test_coordinator.py`: plan review pool runs in parallel, verdicts merged,
  P0 from any reviewer blocks, advisory downgrade works with merged findings
- `test_coordinator.py`: single-reviewer config still works (regression)

## Implementation Notes

The code review pool logic in `coordinator.py` (`_run_review_pool`, verdict
merging) should be extracted or generalized so plan review can reuse it rather
than duplicating. The prompt builder is different (`build_plan_review_prompt`
vs `build_review_prompt`) but the pool execution and merging mechanics are
identical.

## Out of Scope

- Plan review synthesis (merging via LLM) — deterministic merge is sufficient
  for plan review where findings are simpler than code review
- Weighted verdicts (one reviewer's opinion counts more) — all reviewers equal
