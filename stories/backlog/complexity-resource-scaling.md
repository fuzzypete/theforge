---
name: "Adaptive resource budgets — derive iteration and timeout limits from complexity and run history"
slug: complexity-resource-scaling
pytest_target: tests/
depends_on: [domain-aware-routing]
---

# Adaptive Resource Budgets

## Problem

Forge has too many manually authored resource knobs, and none of them adapt.

Today an operator sets `max_iterations`, `timeout_seconds`, and `budget_usd`
per agent, per phase. These are static — the same limits apply whether the
story is a one-file rename or a cross-module concurrency rewrite. The numbers
are arbitrary: codex-reviewer gets 15 iterations, deepseek-reviewer gets 30.
Why? Because someone guessed.

The result is two simultaneous failure modes:
- **Starved on large work** — codex reviewer timed out at 300s on a large
  story (observed). Plan review at 15 iterations cut off mid-thought on a
  high-complexity plan (observed).
- **Wasteful on small work** — 30 iterations for a docs-only rename story
  burns tokens and time for no reason.

The fix is not a better formula. The fix is for forge to derive limits from
two things it already knows: how complex the story is, and how its agents
actually behave.

## What "too many knobs" means concretely

A forge.yaml with 4 reviewers and 3 phases has ~24 manually-authored
resource fields (max_iterations × 4 reviewers, timeout_seconds × 4,
budget_usd × 4, plus dev and plan variants). Every one of these is a guess.
The system already has the data to replace these guesses — it just doesn't
use it.

**Data forge already captures per run:**
- Iterations actually used before submit (per agent, per phase)
- Wall clock time per phase
- Cost per phase
- Complexity score (once domain-aware-routing ships)
- Outcome: DONE, ESCALATE, timeout

**What that data tells you:**
- DeepSeek averages 25 tool calls before submit_review. Codex averages 10.
  Their iteration budgets should reflect this — not be hand-tuned.
- Complexity-8 stories take ~2× the iterations of complexity-4 stories.
  Limits should scale accordingly — not be static.
- A reviewer that always finishes in 12 iterations doesn't need 30.
  A reviewer that regularly hits 28 out of 30 is being starved.

## Solution

Replace manually-authored limits with derived limits. The operator expresses
intent (story-level budget, acceptable risk), and forge computes effective
limits per phase per agent from complexity score and observed agent profiles.

### Story-level budget as the primary control

One knob replaces many:

```yaml
assignment:
  budget_per_story_usd: 15.00
```

From this and the complexity score, forge allocates budget across phases.
High complexity → more budget to dev and review. Low complexity → tighter
limits everywhere. The phase split is derived, not configured.

### Agent behavior profiles (learned)

Forge maintains a lightweight profile per agent in
`.forge/agent_profiles.yaml`, updated after each completed run:

- **median_iterations**: how many iterations this agent typically uses
  before finishing (rolling window, last 20 runs)
- **p90_iterations**: the 90th percentile — used as the effective ceiling
- **median_wall_time**: typical wall time per phase
- **timeout_rate**: fraction of runs that hit the timeout (indicates
  the limit is too tight)

New agents start with conservative defaults (the current manually-authored
values, or generous fallbacks). As runs accumulate, the profile converges
and manual values become unnecessary.

### Complexity-scaled limits (derived)

Given the complexity score (1-10) and the agent's behavior profile:

```
effective_iterations = agent.p90_iterations × complexity_factor(score)
effective_timeout    = agent.median_wall_time × complexity_factor(score) × headroom
```

The complexity factor and headroom multiplier are internal — not
configurable, not exposed as YAML fields. They exist to give agents
proportionally more room on harder stories, not to give operators another
knob to tune.

### Interaction with existing stories

- **domain-aware-routing** provides the complexity score (1-10) and domain
  tag. This story consumes both.
- **adaptive-model-assignment** selects WHICH model. This story decides
  HOW MUCH runway that model gets. They compose.
- **escalation-learning** decides when to swap models based on failure
  history. This story adjusts the limits that determine whether a run
  is a failure in the first place. They compose.
- **progress-aware-timeouts** provides early exit when an agent is stuck.
  This story sets the ceiling that stuck detection operates within.

### Backward compatibility

- Explicit per-agent limits in forge.yaml are respected as hard ceilings.
  Derived limits never exceed them.
- When no run history exists, derived limits equal the current authored
  values (or conservative fallbacks). Behavior is unchanged on day one.
- `assignment.adaptive_limits: false` disables derivation entirely.

## Acceptance criteria

- Effective limits for each agent and phase are logged at run start
- When run history exists, iteration and timeout limits are derived from
  agent behavior profiles and complexity score — not from static config
- Agent profiles update after each completed run (rolling window)
- New agents with no history use conservative defaults (current static
  values as fallback)
- Story-level budget is allocated across phases proportional to complexity
- Explicit per-agent limits in forge.yaml act as hard ceilings
- Derived limits never exceed story-level budget
- A reviewer with high timeout_rate gets a higher timeout on next run
- A reviewer that consistently finishes early gets a tighter limit
- Behavior is unchanged when no run history exists (first run)
- Derivation can be disabled via config
- All existing tests pass
- New tests for: profile updates, derivation from history, complexity
  scaling, ceiling behavior, fallback when no history, budget allocation
