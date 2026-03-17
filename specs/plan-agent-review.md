---
name: "Agent review of plan before dev"
slug: plan-agent-review
pytest_target: tests/
---

# Agent Review of Plan Before Dev

## Problem

The PLAN phase produces `forge_plan.md` and DEV consumes it automatically. A bad
plan — wrong approach, missed requirements, hallucinated APIs — poisons the entire
dev run and only surfaces as P1 findings after multiple expensive cycles.

The fix is not a human gate. It's a fast, cheap agent review of the plan before
any dev budget is spent. The same way reviewers catch bad code, a plan reviewer
catches bad plans.

## Requirements

1. After PLAN produces `forge_plan.md`, an agent reviews it against the story
   before DEV runs
2. The plan reviewer receives: the original story and the generated plan
3. The reviewer produces a verdict: APPROVE (plan looks sound) or REJECT (plan
   has problems that will cause dev to fail)
4. On APPROVE, the pipeline continues to DEV as normal
5. On REJECT, the plan is regenerated once and reviewed again — if it fails
   again, the run escalates
6. Plan review uses a lightweight/fast model profile, not the full review pool
7. Plan review is opt-in via forge.yaml — disabled by default, no behaviour
   change for existing projects
8. When plan is injected via `--plan`, plan review is skipped (human already
   reviewed it)

## Acceptance Criteria

- [ ] When enabled, a plan review agent runs after PLAN and before DEV
- [ ] APPROVE verdict proceeds to DEV with no delay
- [ ] REJECT verdict triggers plan regeneration (once), then re-reviews
- [ ] Two consecutive REJECTs escalate the run with the rejection reason
- [ ] Plan review cost appears in the audit log
- [ ] `--plan` injection skips plan review
- [ ] `plan_agent_review.enabled: false` by default — existing runs unaffected
- [ ] All existing tests pass
