---
name: "Plan regen: disposition-aware backtrack prompt"
slug: plan-regen-backtrack-prompt
depends_on: [plan-regen-trajectory-tracking]
pytest_target: tests/test_coord_plan.py
---

# Plan regen: disposition-aware backtrack prompt

## Problem

The current regen prompt always says "fix these findings, do not rewrite from
scratch." When the plan is diverging — same issue family surviving, complexity
growing — this instruction actively prevents the planner from escaping the hole.
Four patch attempts on a fundamentally wrong approach wastes $3+ and never
converges.

## Expected behavior

The regen prompt varies based on `plan_regen_disposition` computed by the
trajectory tracker:

**patch** (current behavior, unchanged):
```
You wrote the plan below. Reviewers found issues. Fix your plan to address
every P1 and P2 finding. Do not rewrite from scratch — make targeted edits.
```

**backtrack** (new):
```
Your plan has been rejected {N} times. The current approach is not converging.

Learned constraints (validated truths from reviewer findings across all attempts):
- {deduped constraint list}

Rejected strategy:
- {one-line summary of the failing architectural decision, e.g.
  "threading strict_auth parameter through load_config and callers"}

Do not patch the current plan. Do not repeat the rejected strategy.
Produce a new plan that satisfies the learned constraints using a different
approach. Prefer the smallest design that works.
```

**escalate**: coordinator escalates to human — no regen prompt issued.

The "learned constraints" are derived from finding themes that have survived
across attempts — things the reviewers have consistently flagged as true
regardless of plan version. The "rejected strategy" is the coordinator's
one-line summary of the dominant surviving theme.

The attempt budget changes shape:
- attempts 0→1: patch loop (up to `max_plan_regen_attempts`, default 2)
- if backtrack disposition detected: one backtrack attempt
- if backtrack attempt still diverges: escalate

## Acceptance criteria

- Regen prompt uses patch framing when disposition is `patch`
- Regen prompt uses backtrack framing when disposition is `backtrack`,
  including learned constraints and rejected strategy summary
- Coordinator escalates after one failed backtrack attempt rather than
  continuing the patch loop
- `max_plan_regen_attempts` still controls the patch loop ceiling
- Backtrack prompt includes all deduped finding themes across prior attempts
- All existing tests pass
- New tests cover prompt content for each disposition, and early escalation
  after failed backtrack
