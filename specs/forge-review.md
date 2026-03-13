---
name: "forge review subcommand — review-only mode on existing worktree"
slug: forge-review
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# forge review — Review-Only Mode

## Problem

`forge run` always starts from INIT. If PREFLIGHT returns ALREADY_DONE, the
run exits before review. This makes it impossible to:

1. Run a fresh review on a branch that was manually patched
2. Test a new review pool (e.g. adding Codex) on existing work
3. Get a review after `--auto-merge` has already been set but you want
   a second opinion first

There is no way to say "skip to REVIEW on this branch".

## Context

### What exists

- `forge run specs/foo.md` — full pipeline, always from INIT
- `--auto` / `--interactive` flags on `forge run`
- PREFLIGHT short-circuits to ALREADY_DONE when spec is fully implemented

### What's missing

A `forge review specs/foo.md` subcommand (or `--review-only` flag) that:
- Skips WORKSPACE creation (assumes worktree exists)
- Skips PREFLIGHT
- Skips DEV + VALIDATE
- Goes directly to REVIEW on the current worktree HEAD
- Writes a `forge_audit.yaml` with the review findings

## Design

### CLI: `forge review`

New subcommand in `cli.py`:

```
forge review <spec> [--worktree PATH]
```

- `spec`: path to spec file (same as `forge run`)
- `--worktree PATH`: optional explicit worktree path. If omitted, uses
  `config.workspace.path_pattern.format(slug=task.slug)` — same as
  `forge run` would use

### Coordinator: `run_review_only()`

New function in `coordinator.py`:

```python
def run_review_only(
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
) -> CoordinatorResult:
    """Run only the REVIEW phase on an existing worktree.

    Skips WORKSPACE, PREFLIGHT, DEV, VALIDATE.
    Returns a CoordinatorResult with phase=DONE (APPROVE) or ESCALATE
    (REQUEST_CHANGES after max cycles exhausted).
    """
```

Behavior:
1. Verify `workspace_path` exists — if not, error immediately with a
   clear message: "Worktree not found at {path}. Run `forge run` first."
2. Get the git diff (`_get_diff`) for the review prompt
3. Run the review pool (same as in `run_task`)
4. If APPROVE → return success, phase=DONE
5. If REQUEST_CHANGES → log findings, return failure, phase=ESCALATE
   (no DEV retry — review-only means review-only)
6. Write audit log

### State machine

`run_review_only` does not use the full state machine loop. It's a
simplified single-pass:

```
INIT → REVIEW → DONE | ESCALATE
```

No retry on REQUEST_CHANGES — that would require a dev agent. The caller
(human) decides what to do with findings.

### Audit log

`generate_audit_log()` already handles the case. `run_review_only` sets:

```python
state.phase = Phase.REVIEW  # or DONE/ESCALATE after
state.review_cycle = 1
state.dev_iteration = 0  # no dev cycles
```

The audit will clearly show `dev_iterations: 0`, `review_cycles: 1`.

### Error cases

- Worktree path doesn't exist → error, exit 1
- Review returns REQUEST_CHANGES → phase=ESCALATE, exit 1, findings in audit
- All reviewers fail → same escalation as `run_task`

## Acceptance Criteria

1. `forge review specs/foo.md` runs only the review pool on the existing
   worktree for slug `foo`
2. PREFLIGHT, DEV, VALIDATE are not run
3. If worktree doesn't exist, error message directs user to `forge run`
4. APPROVE → exit 0, audit written
5. REQUEST_CHANGES → exit 1, audit with findings written
6. `--worktree PATH` overrides the default worktree path
7. Works with multi-model review pool (same as `forge run`)

## Test Expectations

In `tests/test_coordinator.py`:

- `test_review_only_approve` — mock review returns APPROVE → success,
  phase=DONE, dev_iteration=0
- `test_review_only_request_changes` — mock review returns REQUEST_CHANGES
  → failure, phase=ESCALATE, findings in result
- `test_review_only_missing_worktree` — workspace_path doesn't exist →
  error result, clear message
- `test_review_only_no_dev_cycles` — verify state.dev_iteration == 0
  in all cases

## Out of Scope

- Retrying DEV after REQUEST_CHANGES (that's `forge run`)
- Creating a worktree if it doesn't exist
- Modifying PREFLIGHT to accept a `--force` flag (separate enhancement)
