---
name: "forge review: full iteration loop"
slug: forge-review-iterate
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
pytest_target: tests/test_coordinator.py
---

# forge review: Full Iteration Loop

## Problem

`forge review` is a dead-end. It runs the review pool once, prints findings,
and exits. If the review returns REQUEST_CHANGES, the user must manually fix
the code, manually re-run review, and manually merge — defeating the purpose
of having an automated review entry point.

The correct behavior: `forge review` should be a first-class entry point that
starts at REVIEW and then iterates through DEV → VALIDATE → REVIEW as needed,
identical to `forge run` but skipping the initial PREFLIGHT and first DEV pass.

## Design

### New coordinator function: `run_from_review()`

Add `run_from_review(config, task, *, interactive, auto_merge)` that initializes
state with `phase = Phase.REVIEW` and `dev_iteration = 0`, then enters the
existing coordinator loop. This reuses all existing REVIEW, DEV, VALIDATE, and
DONE/ESCALATE logic without duplication.

The initial review is run against the existing worktree as-is. If APPROVE,
auto-merge proceeds. If REQUEST_CHANGES, findings are sent to the dev agent
exactly as in a normal forge run.

### State initialization

```python
state = CoordinatorState(
    phase=Phase.REVIEW,
    dev_iteration=0,
    review_cycle=0,
    preflight_verdict="SKIPPED",  # not run in review-only entry
)
```

### CLI change

`forge review` replaces the call to `run_review_only()` with `run_from_review()`.
The `--worktree` and `--slug` flags are unchanged. Add `--auto-merge` flag
(currently missing from `forge review`).

## Acceptance Criteria

1. `forge review <spec>` with APPROVE verdict → auto-merges if `--auto-merge`
2. `forge review <spec>` with REQUEST_CHANGES → sends findings to dev agent,
   dev fixes, gate runs, re-review runs — full loop
3. `forge review <spec> --auto-merge` flag works (currently `forge review` has
   no `--auto-merge`)
4. Max review cycles and dev iterations are respected (same as `forge run`)
5. `run_review_only()` can be removed or kept as a lower-level utility

## Test Expectations

In `tests/test_coordinator.py`:

- `test_run_from_review_approve_merges` — APPROVE on first review → merge
- `test_run_from_review_request_changes_iterates` — REQUEST_CHANGES → dev
  cycle → re-review → APPROVE
- `test_run_from_review_exhausts_cycles` — REQUEST_CHANGES × max_review_cycles
  → ESCALATE
- `test_run_from_review_skips_preflight` — preflight_verdict is "SKIPPED" in
  audit log, no preflight agent invoked
