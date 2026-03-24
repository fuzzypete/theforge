---
name: "Test coverage gaps — coordinator edge cases and adaptive routing paths"
slug: test-coverage-gaps
pytest_target: tests/
---

# Test Coverage Gaps

## Problem

Several real bugs were caught in production (or by manual review) that had no
regression tests. Each one was discovered by running a sprint and observing
broken behavior, not by the test suite.

## Gaps to cover

### 1. Preflight failure → dev profile actually escalates

When preflight fails (timeout or crash), `state.preflight_complexity` is set
to `"large"` and both `_apply_complexity_adaptation()` and adaptive assignment
should run. Test should assert the resolved `dev_profile` model is the
strong-tier model, not the default — not just that
`state.preflight_complexity == "large"`.

Suggested by Codex review of `8f1ae3c`.

### 2. Preflight failure → PLAN phase fires

When preflight fails, `should_plan` must evaluate to `True` (given
`config.plan.enabled = True`). Currently only tested implicitly through the
complexity fallback commit — needs an explicit assertion.

### 3. DAG `mark_complete` only fires on merge

`StoryDAG.mark_complete` is only called when a story is `ALREADY_DONE` or
its PR was merged during the sprint. A story that succeeds but whose PR is
opened-not-merged leaves dependents stuck. Test should assert that a
successful story with `result.merge = None` correctly blocks its dependents,
and document this as intentional behavior (not a bug) — or fix it.

### 4. `artifacts-under-forge` uncommitted implementation

The dev agent for `artifacts-under-forge` completed the implementation but
never committed it (gate kept failing, coordinator escalated, dev kept
working). The uncommitted changes in
`.forge/worktrees/artifacts-under-forge/` pass all tests. Those changes
should be committed and the new tests they added should be part of the suite
going forward.

### 5. Unknown preflight complexity → plan skipped

If preflight returns a complexity value the coordinator doesn't recognise
(neither `"low"`, `"medium"`, nor `"large"`), `should_plan` silently
evaluates to `False`. Needs an explicit test and a fallback to `"large"` for
unrecognised values (same conservative default as failure).

### 6. `smart_config_models` override logging

When `smart_config_models` overrides an explicitly configured model, it
should log a warning. Currently silent. Needs a test asserting the warning
is emitted.

## Acceptance criteria

- New tests in `tests/test_coordinator.py` and/or `tests/test_coord_phases.py`
  covering gaps 1–3 and 5–6
- `artifacts-under-forge` worktree changes committed and its new tests
  included (gap 4)
- All existing tests continue to pass
- No new production code changes beyond what is strictly needed to make the
  tests pass
