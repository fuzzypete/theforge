---
name: "Auto-commit out-of-scope dirty files after gate PASS"
slug: dirty-worktree-auto-commit
file_scope:
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Dirty Worktree Auto-Commit

## Problem

After a gate PASS, the coordinator checks for dirty files. If any are found
it sends the agent back to DEV for a cleanup iteration. But `make fmt` often
reformats files outside `file_scope` (e.g. `ideate.py` gets a whitespace
fix). These are harmless side-effects that burn a full DEV iteration to clean.

## Solution

After gate PASS, before triggering a DEV retry for dirty worktree:

1. Get the list of dirty files via `git status --porcelain`
2. Parse only `M`, `A`, `D` status lines (skip `R`, `C`, `?`, `!`)
3. Separate into: **in-scope** (file is in `task.file_scope`) and
   **out-of-scope** (file is NOT in `task.file_scope`)
4. If `task.file_scope` is empty, treat all dirty files as in-scope (existing
   behavior — no change)
5. If all dirty files are out-of-scope:
   - Run `git add <out-of-scope files> && git commit -m "chore: auto-commit fmt side-effects"`
   - Log: `"Auto-committed N out-of-scope fmt side-effects: <files>"`
   - Continue to REVIEW — do NOT send back to DEV
6. If any dirty file is in-scope:
   - Existing behavior: send back to DEV for cleanup

## Git status parsing

Use `git status --porcelain` output. Parse each line:
- Column 1+2: XY status codes
- Strip status prefix, handle renamed files (`R old -> new` → take the new name)
- A file is "dirty" if XY is not `??` (untracked) or `!!` (ignored)

Use `_run_shell()` to run git commands in the worktree directory.

## Acceptance Criteria

- [ ] After gate PASS: if all dirty files are outside `file_scope`, auto-commit
      them and proceed to REVIEW without a DEV retry
- [ ] After gate PASS: if any dirty file is inside `file_scope`, still send to
      DEV (unchanged behavior)
- [ ] If `file_scope` is empty, treat all dirty files as in-scope (unchanged)
- [ ] Auto-commit message: `"chore: auto-commit fmt side-effects"`
- [ ] Log message includes count and filenames of auto-committed files
- [ ] Git failures during auto-commit are logged and fall back to existing
      dirty-worktree DEV retry (fail safe)
- [ ] Renamed files (`R` status) handled correctly — use new filename
- [ ] Untracked (`??`) and ignored (`!!`) files are not auto-committed
- [ ] Existing tests pass without modification
- [ ] New test: all dirty files out-of-scope → auto-commit, no DEV retry
- [ ] New test: one dirty file in-scope → DEV retry (unchanged behavior)
- [ ] New test: empty file_scope → all dirty treated as in-scope
