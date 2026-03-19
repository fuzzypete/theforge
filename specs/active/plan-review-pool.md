---
name: "Plan review pool — multi-model plan review"
slug: plan-review-pool
pytest_target: tests/
---

# Plan Review Pool

## Problem

Plan review (`plan_agent_review`) currently supports only a single reviewer. Code
review (`review_pool`) already supports multiple concurrent reviewers with deterministic
verdict merging. Plan review deserves the same treatment — it's the most important
quality gate because a bad plan burns expensive dev iterations.

The coordinator already half-expresses this intent: it constructs `par_profile` with
`allowed_tools` from the preflight profile, and the plan review prompt assumes tool
access. But the config only accepts a single profile.

## Goal

Extend `plan_agent_review` to support a pool of reviewers (same pattern as
`review_pool`), with deterministic verdict merging. A single reviewer remains valid
as a pool of one.

## Acceptance Criteria

### AC-1: Config — plan_agent_review accepts a pool

`forge.yaml` supports both the existing single-profile format (backward compat) and
a new pool format:

```yaml
# New pool format:
plan_agent_review:
  enabled: true
  pool:
    - name: opus-plan-reviewer
      cli: claude
      model: opus
      budget_usd: 2.00
      timeout: 600
      allowed_tools: [Read, Bash, Glob, Grep]
    - name: codex-plan-reviewer
      provider: openai
      model: gpt-5.1-codex-mini
      budget_usd: 1.00
      timeout: 120
      allowed_tools: [Read, Glob, Grep]

# Legacy single format still works:
plan_agent_review:
  enabled: true
  cli: claude
  model: opus
  budget_usd: 2.00
  timeout: 600
```

When the legacy format is detected (no `pool` key), it is internally converted to
a pool of one.

### AC-2: Coordinator runs plan reviewers concurrently

The coordinator dispatches plan review via `run_agent_pool()` (same as code review),
passing the plan review prompt to all pool profiles. Results are collected in parallel.

### AC-3: Deterministic verdict merging for plan review

Plan review verdicts are merged mechanically:
- Any P0 finding from any reviewer → merged verdict is `REJECT`
- No P0 findings → merged verdict is `APPROVE` (P1s are advisory, passed to dev)
- Parse errors from any reviewer → that reviewer's result is excluded (with warning)
- All reviewers failed → merged verdict is `REJECT` with parse error

This matches the existing plan review semantics in the coordinator (P0 blocks,
P1 is advisory downgrade).

### AC-4: Plan review findings merged across reviewers

When the merged verdict is APPROVE with advisory findings (P1s), findings from all
reviewers are concatenated and passed to the dev agent via
`state.plan_agent_review_findings`. Each finding is prefixed with the reviewer name
for attribution.

### AC-5: Session resume per pool reviewer

Each plan reviewer gets its own session ID tracked in `state.plan_review_session_ids`
(a dict mapping reviewer name → session_id), following the same pattern as
`state.reviewer_session_ids` for code review.

### AC-6: Cost tracking

Per-reviewer cost is tracked and enforced. Budget exceeded on any reviewer escalates
(same as code review budget enforcement).

### AC-7: PlanAgentReviewConfig changes

`PlanAgentReviewConfig` gains a `pool: list[ModelProfile]` field. The existing fields
(`cli`, `model`, `budget_usd`, `timeout`) become optional — present only for legacy
single-profile configs. A `profiles` property returns the pool list regardless of
format.

### AC-8: Tests

- `test_config.py`: pool format loads correctly, legacy format converts to pool of one
- `test_coordinator.py`: plan review pool runs concurrently, verdicts merged correctly
- `test_coordinator.py`: P0 from one reviewer + APPROVE from another → REJECT
- `test_coordinator.py`: all reviewers fail → REJECT with parse errors
- `test_coordinator.py`: advisory P1s from multiple reviewers concatenated in dev prompt

## Implementation Notes

### Merge function

Create `merge_plan_review_results()` in `review.py`, analogous to
`merge_review_results()`. Simpler because plan review has fewer fields (verdict,
findings — no spec_compliance or test_coverage).

### Coordinator changes

The plan review section in `coordinator.py` currently constructs a single `par_profile`
and calls `run_agent()`. Replace with:
1. Build profiles from `config.plan_agent_review.profiles`
2. Call `run_agent_pool()` with all profiles
3. Parse results, merge verdicts
4. Apply advisory downgrade logic on merged result

### Logging

```
[forge] ▸ PLAN_REVIEW   opus+codex-mini  (2 reviewers)
[forge]   ✓ PLAN_REVIEW   approve (merged, 3 advisory)  $1.45  35s
```

## Out of Scope

- Review role specialization for plan review (future)
- Synthesis agent for plan review (deterministic merge is sufficient)
- Changing the plan generation phase (stays single-agent)
