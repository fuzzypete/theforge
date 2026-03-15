---
name: "file_scope advisory — auto-commit fmt side-effects, soften enforcement"
slug: file-scope-advisory
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/task.py
  - tests/test_coordinator.py
tests_target: tests/
---

# file_scope Advisory Mode

## Problem

`file_scope` was designed to constrain agents to specific files, but the
rigid enforcement causes more problems than it solves:

1. **Dirty worktree false positives** — `make fmt` reformats files outside
   `file_scope` (e.g. `ideate.py`). These harmless whitespace changes trigger
   the dirty worktree check, sending the agent back for a pointless cleanup
   iteration that burns time and money.

2. **Impossible tasks burn full budget** — If a spec omits a required file
   from `file_scope`, the agent can't implement it correctly but tries anyway.
   This costs 5 review cycles before escalation. (Partially addressed by
   `preflight-scope-check` and `dev-scope-escalation`, but the root issue is
   the rigidity itself.)

3. **Spec authoring burden** — Writers must perfectly predict every file that
   needs changing. This is impractical for cross-cutting changes.

The review process already catches unauthorized changes. Reviewers will flag
an unexpected edit to an unrelated module as a P1. Hard enforcement in the
coordinator adds friction without adding safety beyond what review provides.

## Solution

### 1. Dirty worktree check: auto-commit out-of-scope fmt changes

After a gate PASS, before flagging a dirty worktree and sending back to DEV:

1. Check which files are dirty
2. If ALL dirty files are outside `file_scope` (or `file_scope` is empty):
   - Run `git add <dirty files> && git commit -m "chore: auto-commit fmt side-effects"`
   - Continue to REVIEW — do NOT send back to DEV
3. If ANY dirty file is inside `file_scope`:
   - Still send back to DEV (the agent forgot to commit its own work)

This eliminates the ruff/formatter false-positive loop entirely.

### 2. Dev prompt: change file_scope from hard stop to guidance

**Current (enforcement):**
```
You may ONLY create or modify files in these locations:
- src/theforge/coordinator.py
...
If the task requires changes outside this list, STOP...
```

**Replace with (advisory):**
```
Focus your changes on these files:
- src/theforge/coordinator.py
...
If you need to touch a file not listed here, do so — but keep changes
minimal and directly related to the spec. The reviewer will flag any
unexpected changes.
```

Remove the `SCOPE_BLOCKED` instruction from `build_dev_prompt()`. The
`_parse_scope_blocked()` and coordinator detection can remain for cases where
the agent explicitly signals it (optional use), but it should not be the
primary enforcement mechanism.

## What file_scope still does (keep these)

- **Preflight context**: preflight reads these files to assess current state
- **Reviewer signal**: reviewers see what files were intended to change and
  can flag unexpected edits as P2/P1
- **Preflight scope-check**: preflight warns if spec text requires a file not
  listed (informational, not a hard block — return PROCEED with a warning)

## Acceptance Criteria

- [ ] Dirty worktree check: if all dirty files are outside `file_scope`,
      auto-commit them with `chore: auto-commit fmt side-effects` and proceed
      to REVIEW without sending back to DEV
- [ ] Dirty worktree check: if any dirty file IS in `file_scope`, still send
      back to DEV (agent has uncommitted scope work)
- [ ] `build_dev_prompt()` file_scope section changed from "ONLY" enforcement
      to advisory guidance language
- [ ] `SCOPE_BLOCKED` block removed from `build_dev_prompt()`
- [ ] `file_scope` still passed to preflight for context (unchanged)
- [ ] Existing tests pass
- [ ] New test: dirty worktree with only out-of-scope files → auto-commit,
      no DEV retry triggered
- [ ] New test: dirty worktree with in-scope dirty file → DEV retry still
      triggered
