---
name: "Plan model escalation on repeated plan review rejection"
slug: plan-escalation
pytest_target: tests/
---

# Plan Model Escalation

## Problem

When plan review rejects the plan repeatedly, the coordinator regenerates with
the same model until max_plan_regen_attempts is exhausted, then escalates the
entire story. There's no model escalation for the planner — unlike DEV which
promotes sonnet→opus on persistent P1s.

Observed in HDP: redesign-home-screen plan was rejected 4 times by plan review.
Sonnet kept regenerating the same insufficient plan. Opus planning would likely
have produced a plan that passed, but the coordinator never tried.

## Solution

After N consecutive plan review rejections (configurable, default 2), escalate
the planner model to the next tier before regenerating. Same pattern as dev
model escalation:

1. Plan review rejects
2. Plan regenerated with same model
3. Plan review rejects again → persistent rejection detected
4. Escalate planner to next tier (e.g. sonnet → opus)
5. Regenerate plan with upgraded model
6. If still rejected after escalation, exhaust attempts and escalate story

## Acceptance criteria

- After N consecutive plan rejections, planner model is escalated to next tier
- Escalation threshold is configurable (default: 2 rejections)
- Escalation note injected into planner prompt ("previous plan was rejected N times, key findings: ...")
- Plan review findings from prior attempts are passed to the upgraded planner
- If no higher tier is available, continue with current model
- Escalation is logged in telemetry
- All existing tests pass
- New test: 2 plan rejections trigger model escalation
