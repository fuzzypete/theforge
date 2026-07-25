# Dev/Review Guide

This guide covers the contract between the dev and review phases in the normal
TheForge story loop.

## P1 vs P2

- P1 findings remain blocking. Any unresolved P1 keeps the review verdict at
  `REQUEST_CHANGES`.
- P2 findings do not block merge by themselves, but they are not "free to
  ignore" by default.

## Dev P2 policy

The active mode comes from `forge.yaml`:

```yaml
dev:
  p2_policy: in_scope
```

Supported values:

- `in_scope` (default): if a P2 touches code the dev agent is already changing,
  or adjacent code it must inspect to finish the change safely, that P2 belongs
  in the current run and should be fixed now.
- `all`: every open P2 the dev agent encounters is in-scope for the run.
- `p1_only`: legacy behavior; only P1s are required unless a P2 must be fixed
  to complete the story safely or avoid a regression.

## Reviewer guidance

- Reviewers should report the defect they found and why it matters.
- Reviewers should not use phrasing like "pre-existing, so skip it" or
  "out of scope" as binding permission for the next dev agent to ignore a P2.
- Whether a P2 is in-scope for the current run is decided by the dev agent
  using the active `dev.p2_policy` and the proximity of that finding to the
  code being changed.

## Operator visibility

The coordinator logs the active `dev.p2_policy` at run start so the operator
can see which mode governed the run.
