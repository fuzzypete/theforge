---
name: "AI-assisted escalation — LLM assessment before terminal decisions"
slug: ai-escalation-assessment
pytest_target: tests/
depends_on: [escalation-controls]
---

# AI-Assisted Escalation Assessment

## Problem

When a story escalates (persistent P1s, max cycles), the coordinator makes a
binary decision with no analysis. Sometimes P1s are reviewer hallucinations.
Sometimes a model switch would fix the problem. A quick LLM assessment before
the terminal decision could save budget and avoid unnecessary human intervention.

## Solution

Add an optional assessment step to escalation policies:

```yaml
escalation:
  on_persistent_p1: assess_then_decide
  assessment_model: sonnet  # cheap, fast assessment
```

When triggered, the coordinator sends the escalation context (findings, dev
handoff, cycle history) to a cheap model and asks:
- Are the P1 findings real or hallucinated?
- Would a model upgrade likely fix the issue?
- Is the story scope too large for a single dev iteration?

The assessment returns a recommendation. The coordinator still makes the
final decision deterministically based on the recommendation + policy.

## Acceptance criteria

- Assessment step runs before terminal escalation decisions
- Assessment uses a configurable (cheap) model
- Recommendation is structured: {action: retry|escalate_model|reject, reason: ...}
- Coordinator decision is still deterministic (not LLM-in-the-loop for routing)
- Assessment is optional and disabled by default
- Assessment cost tracked in telemetry
- All existing tests pass
