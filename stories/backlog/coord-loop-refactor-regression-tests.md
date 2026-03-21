---
name: coord-loop-refactor-regression-tests
slug: coord-refactor-regressions
---

# Regression Tests for Coordinator Loop Refactor

Add targeted regression tests for two semantics that were broken and restored
during the `coord-loop-refactor` branch.

## 1. Pre-Synthesis Reviewer Budget Enforcement

**What broke:** Reviewer budget enforcement moved after synthesis, meaning an
over-budget reviewer's result could feed into synthesis before being caught.

**What was restored:** Budget check runs per-reviewer before synthesis in
`coord_phases.py`.

**Test:** Mock a review pool run where one reviewer's cumulative cost exceeds
its `budget_usd`. Verify:
- That reviewer's result is excluded from synthesis input
- Synthesis still runs with remaining reviewers (degraded mode)
- Audit log records the budget violation

## 2. Role-Aware `run_review_only()` Generic Prompt

**What broke:** `run_review_only()` inherited role-specific review prompts
from the shared helper, but its contract is a single generic review (no
role specialization).

**What was restored:** `run_review_only()` clears `review_role` on profiles
before delegating to the shared review helper in `coordinator.py`.

**Test:** Call `run_review_only()` with a profile that has `review_role` set.
Verify:
- The prompt passed to `run_agent` does NOT contain role-specific lens text
- The review uses the generic prompt template
