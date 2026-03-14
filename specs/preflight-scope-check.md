---
name: "Preflight scope check for file_scope vs spec requirements"
slug: preflight-scope-check
file_scope:
  - src/theforge/task.py
  - tests/test_task.py
pytest_target: tests/
---

# Preflight Scope Check

## Problem

When a spec's `file_scope` excludes a file that the spec body explicitly
requires modifying, preflight returns PROCEED and the dev agent burns full
budget before failing. The dev agent cannot touch the missing file, builds a
workaround, and reviewers correctly reject it — 5 cycles, $2–7, 20–30 min
wasted on a task that was structurally impossible from the start.

**Observed failure:**
- `reviewer-role-specialization` spec required changing `run_agent_pool()` in
  `runner.py`, but `runner.py` was not in `file_scope`
- Preflight returned PROCEED
- Dev agent built a coordinator-local workaround `_run_pool_with_per_prompts()`
- Reviewers flagged "run_agent_pool() not changed" as P1 all 5 cycles
- $2.21 burned, escalated

## Root Cause

`build_preflight_prompt()` in `task.py` lists BLOCKED conditions as:
- References files/functions/APIs that don't exist
- Conflicts with current architecture
- Unresolvable ambiguities
- Missing dependencies

It does NOT include: **"spec requires modifying a file not in file_scope."**

The preflight agent is not instructed to cross-reference the spec body's
required changes against the `file_scope` list.

## Solution

### 1. Add a new BLOCKED condition to `build_preflight_prompt()`

In the BLOCKED bullet list, add:

```
- The spec explicitly requires modifying a file that is NOT listed in
  file_scope (and file_scope is non-empty). Name the missing file(s) in
  your reason.
```

### 2. Add a scope-check instruction paragraph

Below the BLOCKED condition list, add explicit guidance:

```
## Scope Feasibility Check

If file_scope is non-empty, scan the spec body for files explicitly named
as requiring modification (look for file paths, function signatures tied to
specific files, "in X.py change Y", acceptance criteria referencing specific
files). For each such file, verify it appears in the file_scope list below.
If any required file is absent from file_scope, verdict is BLOCKED — do not
return PROCEED.

This check is mandatory when file_scope is non-empty.
```

### 3. Pass file_scope explicitly in the prompt (it already appears implicitly)

The file_scope is already shown via the "Current File Contents" block, but
add it as a named structured section so the agent can reference it clearly
during the scope feasibility check:

```
## File Scope (the only files the dev agent may modify)

{file_scope_str}
```

This should appear BEFORE the "Current File Contents" block so the agent
knows what the scope is before reading the files.

## Acceptance Criteria

- [ ] BLOCKED condition list in `build_preflight_prompt()` includes the
      file_scope mismatch case
- [ ] Explicit "Scope Feasibility Check" instruction paragraph added
- [ ] File scope listed as a named section before file contents
- [ ] When file_scope is empty (no restriction), scope check is skipped
- [ ] Existing tests pass without modification
- [ ] New test: preflight prompt includes scope feasibility instruction when
      file_scope is non-empty
- [ ] New test: preflight prompt omits scope check instruction when
      file_scope is empty
