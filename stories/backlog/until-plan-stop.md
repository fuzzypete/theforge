---
name: "fix: --until plan flag has no effect — pipeline runs past plan phase"
slug: until-plan-stop
---

# fix: --until plan flag has no effect — pipeline runs past plan phase

## Problem

`forge run <story> --until plan` is supposed to stop the pipeline after the plan
phase completes. Instead it runs the full pipeline (DEV, VALIDATE, REVIEW) and
only prints "Stopped at --until plan" after the fact. The flag is cosmetic — it
has no effect on execution.

Reported as GH issue #196. A real run cost $19.14 when it should have cost ~$3
(plan only).

## Goal

When `--until plan` (or `--until plan-review`) is passed, the coordinator must
stop before entering the DEV phase. No dev agent is invoked, no gate runs, no
review pool runs. The run exits successfully with an appropriate message.

## Acceptance Criteria

- `forge run <story> --until plan` exits after PLAN_REVIEW completes and before
  DEV starts. Exit code is 0.
- `forge run <story> --until plan-review` behaves identically to `--until plan`
  (both names are already accepted by the CLI; both must stop at the same point).
- `forge run <story> --until dev` still enters DEV and stops before VALIDATE, as
  before.
- `forge run <story> --until validate` still enters VALIDATE and stops before
  REVIEW, as before.
- A unit test covers the `--until plan` case: coordinator returns before DEV is
  invoked, with `phase == Phase.PLAN_REVIEW` (or `Phase.PLAN` when plan review is
  disabled) and `success == True`.
- A unit test covers `--until dev` to guard against regression.
- All existing tests pass.
