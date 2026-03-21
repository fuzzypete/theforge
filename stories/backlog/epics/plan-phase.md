# Epic: PLAN Phase — Implementation Planning Between Preflight and Dev

## Vision

The dev agent currently receives a spec and must interpret it directly into
code. When specs are underspecified, it makes assumptions. Reviewers catch
wrong assumptions. The cycle repeats until budget is exhausted.

A PLAN phase inserts an architectural step (Opus-class model) that reads the
spec + codebase and produces a structured implementation plan. The dev agent
executes the plan, not the spec. This mirrors the manual workflow of
discovery → planning → implementation that consistently produces better
first-pass code.

## Stories

### Phase 1: Core implementation
- [x] `plan-phase.md` (brief: `briefs/plan-phase.md`) — Phase.PLAN in state
      machine, build_plan_prompt(), PlanConfig, forge_plan.md output,
      plan_output flows into build_dev_prompt()

### Phase 2: Robustness
- [ ] Plan agent commits `forge_plan.md` (currently left as untracked file,
      triggers dirty worktree detection) — needs spec
- [ ] Plan-aware retry: on DEV retry with REQUEST_CHANGES, should the plan
      be updated or used as-is with review findings? — needs spec
- [ ] Plan validation: lightweight check that the plan covers all acceptance
      criteria from the spec — needs spec

### Phase 3: Optimization
- [ ] Skip PLAN for small complexity (currently runs for medium+large only,
      but the complexity threshold may need tuning)
- [ ] Plan caching: reuse plan across retries within the same review cycle
      (currently re-reads from forge_plan.md, which is correct)

## Definition of Done

- PLAN phase runs for medium/large specs, produces a plan the dev agent follows
- forge_plan.md is committed (not left dirty)
- Plan-aware retry reduces wasted cycles on ambiguous specs
- First-pass APPROVE rate improves measurably vs. no-plan runs
