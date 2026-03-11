---
name: "Budget enforcement for agent invocations"
slug: budget-enforcement
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/runner.py
  - tests/test_coordinator.py
  - tests/test_runner.py
pytest_target: tests/
---

# Budget Enforcement

## Problem

`ModelProfile.budget_usd` exists in config but is never checked. An agent could
run up an unlimited bill if it loops or hangs.

## Requirements

1. After each `run_agent()` call in the coordinator, check cumulative cost
   against the profile's budget. If total dev cost exceeds `dev_profile.budget_usd`
   OR total review cost exceeds `review_profile.budget_usd`, escalate immediately
   with a clear error message including actual vs allowed spend.

2. The cost check should use `AgentResult.cost_usd` which is already captured.

3. Add a `total_review_cost` property to `CoordinatorState` alongside the
   existing `total_agent_cost` (which only sums dev results). Rename
   `total_agent_cost` to `total_dev_cost` for clarity.

4. Add tests:
   - Dev agent exceeds budget on first call → ESCALATE
   - Dev agent exceeds budget on retry → ESCALATE
   - Review agent exceeds budget → ESCALATE
   - Costs within budget → normal flow continues

## Non-goals

- Per-invocation budget limits (only cumulative)
- Budget warnings before hitting the limit
