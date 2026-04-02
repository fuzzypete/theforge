---
name: "Adversarial plan review for algorithmic stories"
slug: adversarial-plan-review
github_issue: 262
pytest_target: tests/
---

# Adversarial Plan Review for Algorithmic Stories

## Problem

Plan review currently instructs reviewers to "default to APPROVE" and
explicitly says "Do NOT evaluate hypothetical edge cases that the dev agent
can handle at implementation time." This is correct for most stories — wiring,
config, plumbing — but catastrophic for stories whose core value is an
algorithm or policy decision.

The plan-review-corroboration story burned 3 code review cycles because the
plan described a component-level grouping algorithm that subtly violated a
per-finding AC. All 3 plan reviewers approved. All 3 code reviewers
independently caught it by constructing counterexamples against real code.
The plan review prompt actively discouraged the behavior that would have
caught this at plan time.

## Goal

When preflight classifies a story as algorithmic or policy work, plan review
switches to an adversarial mode. Reviewers are instructed to construct minimal
counterexamples against the proposed algorithm rather than just checking
structural coherence and AC coverage. The default APPROVE bias is removed for
these stories.

## Acceptance Criteria

- Preflight work_type vocabulary gains a new value (e.g. "policy") or a
  secondary flag (e.g. "algorithmic: true") that identifies stories whose
  core value is a classification, gating, matching, ranking, or synthesis
  algorithm
- When the story is flagged as algorithmic, build_plan_review_prompt injects
  an additional evaluation step: "For each algorithmic AC, construct one
  minimal counterexample that would distinguish a correct implementation from
  a plausible-but-wrong one. If you cannot falsify the plan, say so
  explicitly."
- The "Do NOT evaluate hypothetical edge cases" instruction is scoped to
  non-algorithmic stories only, or reworded so it does not suppress
  counterexample construction for algorithmic ACs
- Non-algorithmic stories (wiring, config, plumbing) see no change in plan
  review behavior
- All existing tests pass

## Non-goals

- Changing code review prompts (post-dev review is already adversarial enough)
- Changing preflight prompts beyond adding the classification signal
- Changing plan review for non-algorithmic stories
- Automated detection of "algorithmic" stories from code analysis — this is a
  preflight classification from the story text
