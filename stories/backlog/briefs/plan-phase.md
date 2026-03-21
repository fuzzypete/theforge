# Brief: PLAN Phase — Implementation Planning Between Preflight and Dev

## Background

TheForge's current state machine is:

```
INIT → WORKSPACE → PREFLIGHT → DEV → VALIDATE → REVIEW → DONE/ESCALATE
```

A critical phase is missing between PREFLIGHT and DEV. The dev agent currently
receives a spec and must interpret it directly into code. When specs are
underspecified (missing edge cases, ambiguous implementation approach, unclear
API contracts), the dev agent makes assumptions. Reviewers catch the wrong
assumptions. The agent retries with different assumptions. This cycle repeats
until budget is exhausted.

## The Manual Practice That Works

Before theforge, the project owner used a manual workflow:

1. **Discovery session** — Opus (+ sometimes Codex high-reasoning, Gemini)
   reads the codebase and the problem statement, produces a discovery document:
   exact functions to change, edge cases to handle, test approach, open
   questions resolved.

2. **Planning session** — Opus generates a detailed implementation plan from
   the discovery doc: step-by-step changes, exact code patterns to use, test
   cases to write.

3. **Dev** executes the plan, not the spec.

The reviewer's output also followed this pattern — a clean, structured handoff
that *became* the dev agent's primary input for the next iteration.

This is the SDLC discipline that theforge currently skips.

## What the PLAN Phase Should Do

After PREFLIGHT returns PROCEED, before DEV starts:

- A PLAN agent (Opus preferred — this is an architectural task, not a coding
  task) reads:
  - The spec
  - The current contents of all files in `file_scope`
  - Any preflight output/notes
- Produces a structured **implementation plan** (not code, a plan):
  - Exact functions/methods to add or modify
  - Precise API signatures
  - Edge cases and how to handle each
  - Test cases to write with specific scenarios
  - Ordering of changes (what to write first)
  - Any risks or ambiguities with recommended resolution
- The plan is saved to the worktree as `forge_plan.md`
- DEV agent receives the plan as primary input alongside the spec

## Key Design Questions for Ideation

1. **Always or optional?** Should PLAN run for every spec, or only for
   medium/large complexity? Small specs (single function, clear change) may
   not need it.

2. **Blocking or advisory?** Can the PLAN agent return BLOCKED if the spec is
   too ambiguous to plan? Or is it always advisory?

3. **Plan format** — What structure gives the dev agent the most useful
   input? Step-by-step list? Annotated code sketches? Decision table for
   edge cases?

4. **Plan validation** — Should the plan itself be reviewed/validated, or
   is it trusted as-is from Opus?

5. **Cost/speed tradeoff** — Adding a planning step adds latency and cost.
   How do we keep it lean? Can it share the preflight context window?

6. **Integration with retry** — On DEV retry (review REQUEST_CHANGES), does
   the plan get updated, or does the agent use the original plan plus the
   review findings?

## Existing Infrastructure to Leverage

- `forge ideate` — multi-LLM deliberation, same pattern could be used for
  planning (single Opus call, not multi-model)
- `build_preflight_prompt()` in task.py — similar one-shot classification
  pattern, PLAN follows the same structure
- `preflight_output` — already passed to dev agent, `plan_output` would be
  too
- `CoordinatorState` — already tracks phase, cost, timing per agent

## Expected Output from Ideation

A spec for implementing the PLAN phase in theforge, covering:
- New `PLAN` state in the state machine
- `build_plan_prompt()` in task.py
- Plan agent invocation in coordinator.py
- `plan_output` field in CoordinatorState
- forge.yaml config for plan profile (model, budget, timeout)
- How plan output flows into build_dev_prompt()
- Tests
