---
name: "Preflight classifies spec sufficiency -- skip or soften plan review for implementation-ready specs"
slug: preflight-spec-sufficiency
pytest_target: tests/test_preflight.py
---

# Preflight classifies spec sufficiency

## Problem

Every story goes through the same plan, plan-review, regen pipeline regardless
of how detailed the spec is. A story with comprehensive Notes covering every
implementation pitfall gets the same treatment as a vague two-line feature
request. The most detailed specs -- the ones that least need formal planning --
are the ones most likely to get stuck in plan review, because the more detail
the plan must incorporate, the more surface area for plan reviewers to find
"missing" implementation details.

This is backwards: a spec author who has already done the investigation and
documented the pitfalls is penalized with extra review cycles, while a vague
spec sails through because its plan is correspondingly vague and harder to
critique.

## Expected behavior

Preflight (or an early pipeline stage) classifies whether the spec is
"implementation-ready" -- detailed enough that the plan phase adds little value.
The classification considers signals like: presence of detailed Notes, density
of specific implementation hints, whether acceptance criteria are behavioral
versus prescriptive, and overall spec length relative to the task complexity.

When a spec is classified as implementation-ready, the pipeline adjusts its
treatment. Either the plan phase is skipped entirely (dev receives the spec
directly as its guide), or plan review uses a softened standard where findings
are advisory-only and cannot block. The classification result is recorded in
the preflight output so downstream stages can act on it.

Specs that are vague, ambiguous, or lack implementation context continue through
the full plan-review pipeline unchanged.

## Acceptance criteria

- Preflight output includes a sufficiency classification indicating whether the
  spec is implementation-ready or needs full planning
- The classification considers: presence and detail level of a Notes section,
  specificity of implementation hints, whether acceptance criteria describe
  observable behaviors versus implementation steps, and spec detail relative
  to assessed complexity
- When a spec is classified as implementation-ready, the coordinator either
  skips the plan phase or runs plan review in advisory-only mode (no blocking)
- When a spec is not implementation-ready, the full plan-review pipeline runs
  unchanged
- The sufficiency classification is visible in logs and the preflight output
  structure
- All existing tests pass
- New tests cover: classification of a detailed spec as implementation-ready,
  classification of a vague spec as needing planning, pipeline behavior
  difference for each classification

## Notes

- The preflight prompt already does complexity assessment (small/medium/large).
  Sufficiency classification is a related but orthogonal axis -- a large
  refactor can be implementation-ready if the spec author documented every
  pitfall, and a small feature can need planning if the spec is vague.
- The preflight output YAML already has `verdict`, `complexity`, `reason`,
  `spec_issues`, `warnings`, and `criteria_checked` fields. A new field for
  sufficiency classification would fit naturally here.
- "Advisory-only plan review" could reuse the severity calibration work from
  `plan-review-severity-calibration` -- if that story lands first, this story
  could simply force all plan review findings to the advisory tier when the
  spec is implementation-ready.
- The coordinator's plan flow decides whether to enter the plan-review loop.
  The skip-planning path would need to handle criteria mapping (which normally
  comes from the plan) or accept that implementation-ready specs don't need it.
