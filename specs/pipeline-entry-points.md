---
name: "Pipeline entry points and --until flag"
slug: pipeline-entry-points
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Pipeline Entry Points and --until Flag

## Problem

TheForge's pipeline is a fixed sequence: PREFLIGHT → PLAN → DEV → VALIDATE →
REVIEW → DONE. Today you must run the entire thing or nothing. But the real
workflow has multiple useful stopping points:

- **"Just plan this"** — run PREFLIGHT + PLAN, produce `forge_plan.md`, stop.
  Human reviews the plan before committing to dev cycles. This is the most
  common upstream workflow: ideate → story → plan → review plan → then run.
- **"Just build, I'll review"** — run through DEV + VALIDATE, stop before
  REVIEW. Useful when the human wants to inspect the code before burning
  review budget.
- **"Start from the plan I already have"** — `--plan` injection exists but
  the broader pattern of "enter at phase X" is inconsistent. `forge run`
  always starts at INIT. `forge review` starts at REVIEW. `forge dev` doesn't
  exist but `run_from_dev()` does in the coordinator.

The pipeline should support starting from any phase and stopping at any phase,
with the constraint that skipped upstream phases must have their outputs
available (e.g., you can't start at DEV without a workspace).

## Requirements

1. `forge run <story> --until <phase>` stops after the named phase completes
   successfully, writes the audit log, and exits with success
2. Supported `--until` values: `preflight`, `plan`, `dev`, `review` (the
   default, full pipeline)
3. `--until plan` produces `forge_plan.md` in the worktree and exits — no
   dev agent runs
4. `--until dev` runs through VALIDATE and exits — no review agents run
5. `--until preflight` runs preflight only and exits — useful for checking
   if a story is already done or blocked without spending money
6. When `--until` stops the pipeline early, the audit log records which phase
   was the terminal phase and why (user-requested stop, not failure)
7. `forge sprint` also accepts `--until` and applies it to every story in the
   manifest — useful for batch-planning a sprint without executing it
8. The existing entry points (`forge review`, `forge dev` if added) continue
   to work as shortcuts for common patterns

## Acceptance Criteria

- [ ] `forge run <story> --until plan` exits after PLAN with success, worktree
      contains `forge_plan.md`
- [ ] `forge run <story> --until dev` exits after VALIDATE with success, no
      review agents invoked
- [ ] `forge run <story> --until preflight` exits after PREFLIGHT with the
      verdict logged
- [ ] `forge run <story>` (no flag) runs the full pipeline as today
- [ ] `forge sprint <manifest> --until plan` plans every story without dev
- [ ] Audit log for early-stop runs shows `stopped_at: <phase>` and
      `stop_reason: "user_requested"`
- [ ] `--until plan` on a small-complexity story that skips PLAN exits after
      PREFLIGHT with a clear message (PLAN was skipped, nothing to stop at)
- [ ] Budget tracking works correctly for partial runs
- [ ] `--until` is incompatible with `--auto-merge` (can't merge without
      review) — clear error if both specified
- [ ] Existing tests pass unchanged
