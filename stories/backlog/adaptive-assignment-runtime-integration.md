---
name: Adaptive assignment runtime integration tests
slug: adaptive-assignment-runtime-integration
pytest_target: tests/
depends_on: [adaptive-model-assignment]
---

# Adaptive Assignment Runtime Integration Tests

## Problem

`assign_models()` has direct unit coverage, but the trust boundary for
dogfooding is the coordinator runtime, not the pure selector. The real feature
spans config loading, preflight wiring, plan selection, plan review selection,
review-pool preservation, sprint promotion caching, and post-run history write.

Right now those behaviors are spread across `preflight_flow.py`,
`plan_flow.py`, and `engine.py`, with little coordinator-level proof that the
runtime honors the intended override and persistence semantics.

## Acceptance criteria

- Add coordinator-level integration tests for `assignment.enabled: true`
- Tests prove adaptive planner is used when plan config is default
- Tests prove explicit `plan.model` is preserved when configured
- Tests prove adaptive code-review pool replaces the default review pool
- Tests prove explicit `review_pool` is preserved when configured
- Tests prove adaptive plan reviewers replace `plan_agent_review` only when not
  explicitly configured
- Tests prove escalation history is written after `DONE`
- Tests prove escalation history is written after `ESCALATE`
- Tests assert against real coordinator state/runtime behavior, not only
  `assign_models()` output
- Existing assignment and coordinator tests continue to pass
