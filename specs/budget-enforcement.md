---
name: "Budget enforcement for agent invocations"
slug: budget-enforcement
file_scope:
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Budget Enforcement

## Problem

`ModelProfile.budget_usd` exists in config but is never checked. An agent could
run up an unlimited bill if it loops or hangs.

## Context

`CoordinatorState` already has `total_dev_cost`, `total_review_cost`, and `total_cost`
properties. `AgentResult.cost_usd` is already captured after each agent invocation.

## Requirements

1. After each `run_agent()` call in the coordinator, check cumulative cost against
   the profile's `budget_usd`. If the check fails, escalate immediately with a clear
   error message showing actual vs allowed spend.

   - After `run_agent(profile=config.dev_profile, ...)`: if `state.total_dev_cost >
     config.dev_profile.budget_usd`, escalate.
   - After `run_agent(profile=config.review_profile, ...)`: if `state.total_review_cost >
     config.review_profile.budget_usd`, escalate.

2. The error message must include both actual cost and budget ceiling.

3. Add tests in `tests/test_coordinator.py`:
   - Dev agent exceeds budget on first call → ESCALATE with budget error
   - Dev agent exceeds budget on retry (second call) → ESCALATE
   - Review agent exceeds budget → ESCALATE
   - Costs within budget → normal flow continues (existing tests already cover this)

## Non-goals

- Per-invocation budget limits (only cumulative)
- Budget warnings before hitting the limit
- Modifying `runner.py` (no changes needed there)
