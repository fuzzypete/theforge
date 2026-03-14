---
name: "Dev agent scope escalation — fail fast on out-of-scope requirements"
slug: dev-scope-escalation
file_scope:
  - src/theforge/task.py
  - tests/test_task.py
pytest_target: tests/
---

# Dev Agent Scope Escalation

## Problem

When a dev agent determines that correctly implementing a spec requires
modifying a file outside its `file_scope`, the current prompt says:

> "If the task requires changes outside this list, STOP. Add a note in your
> commit message that scope expansion is needed. Do NOT make out-of-scope
> changes."

In practice, agents interpret "STOP" loosely: they find a workaround within
the allowed scope (e.g., duplicate logic in an in-scope file), commit it, and
let the gate pass. The workaround gets rejected 5 times by reviewers for not
meeting the spec. Full budget burned, task escalates.

**Root cause:** The instruction allows an escape hatch ("add a note in your
commit message") that the agent exploits by committing a workaround instead
of committing nothing. Workarounds pass the gate but fail review.

## Solution

Tighten the scope violation instruction in `build_dev_prompt()` to eliminate
the workaround path:

### Current (permissive)

```
If the task requires changes outside this list, STOP. Add a note in your
commit message that scope expansion is needed and describe what file(s).
Do NOT make out-of-scope changes.
```

### Replacement (strict)

```
**If you determine that correctly implementing this spec requires modifying
a file NOT in the list above:**

1. Do NOT implement a workaround within in-scope files.
2. Do NOT commit any code changes.
3. Output ONLY the following in your final response:

   SCOPE_BLOCKED: Cannot implement spec correctly within file_scope.
   Required files not in scope: <list file paths>
   Reason: <one sentence explaining what each file needs to do>

The coordinator will treat any session that ends with SCOPE_BLOCKED as an
escalation. Workarounds that technically pass the gate but cannot pass review
waste far more budget than an early escalation.
```

## Coordinator change

The coordinator should detect `SCOPE_BLOCKED:` in the dev agent output and
immediately escalate (skip VALIDATE and REVIEW) with a clear error message:

```
ESCALATE: Dev agent scope blocked. Required files: <files>. Update file_scope in spec.
```

This surfaces the spec authoring error in one iteration instead of five
review cycles.

## Acceptance Criteria

- [ ] `build_dev_prompt()` scope instruction replaced with strict version
- [ ] Strict version: no commit, output `SCOPE_BLOCKED:` sentinel
- [ ] Coordinator detects `SCOPE_BLOCKED:` in dev output (check
      `dev_result.output`)
- [ ] On detection: skip VALIDATE, escalate immediately with descriptive
      error naming the blocked files
- [ ] Existing tests pass without modification
- [ ] New test: coordinator escalates immediately on `SCOPE_BLOCKED:` output
- [ ] New test: `build_dev_prompt()` contains the strict scope instruction
