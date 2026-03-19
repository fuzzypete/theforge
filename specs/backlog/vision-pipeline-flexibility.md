---
name: "Vision update: pipeline flexibility and story-first workflow"
slug: vision-pipeline-flexibility
file_scope:
  - docs/vision.md
pytest_target: tests/
gate: none
---

# Vision Update: Pipeline Flexibility and Story-First Workflow

## Problem

The vision doc describes a linear pipeline (PREFLIGHT → DEV → REVIEW → DONE)
and a linear upstream workflow (brief → spec → sprint). Two gaps:

1. **No partial runs.** The pipeline is all-or-nothing. In practice, the most
   valuable workflow is iterative: ideate → story → plan (review the plan) →
   dev → review. The human wants to inspect intermediate artifacts (plans,
   preflight verdicts) before committing budget to downstream phases.

2. **Stories vs specs.** The vision doc and codebase use "spec" for the atomic
   work unit, but specs as currently generated are implementation-heavy design
   docs. The intended artifact is a behavioral story (WHAT/WHY/acceptance
   criteria). The PLAN phase produces the HOW. This distinction should be
   reflected in the vision.

## Requirements

1. Update the pipeline diagram to show that `forge run --until <phase>` can
   stop at any phase boundary
2. Add a section describing the full upstream workflow:
   - Brief → `forge ideate` → Story (behavioral, no implementation detail)
   - Story → `forge run --until plan` → Plan (review before dev)
   - Story → `forge run` → full pipeline
   - Stories → `forge sprint` → batched execution
3. Document the story format contract: stories describe WHAT and WHY,
   acceptance criteria are behavioral, PLAN derives the HOW
4. Note that existing specs in `specs/` are grandfathered — they work and
   don't need to be rewritten, but new stories produced by `forge ideate`
   will follow the lean format
5. Update the terminology table if one exists, or add one:
   brief, story, plan, sprint, batch, epic, track

## Acceptance Criteria

- [ ] Vision doc includes pipeline flexibility (--until) in the current state
      or roadmap section
- [ ] Vision doc describes the upstream workflow (brief → story → plan → dev)
- [ ] Vision doc distinguishes story (WHAT/WHY) from plan (HOW)
- [ ] Terminology is consistent throughout the updated sections
- [ ] No existing content is removed — only additions and clarifications
