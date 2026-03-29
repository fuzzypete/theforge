---
name: "Plan reviewer should not be same model as planner (self-review)"
slug: plan-reviewer-diversity
---

# Plan reviewer should not be same model as planner (self-review)

## Observed

When adaptive assignment selects opus as the planner, opus also ends up reviewing its own plan. The plan reviewer pool makes no attempt to exclude or deprioritize the same model that produced the plan.

## Expected

The plan review pool should prefer reviewers from a different model or provider than the planner. Self-review produces less effective feedback — the same model is unlikely to catch its own blind spots.
