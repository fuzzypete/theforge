---
name: "file_scope advisory — soften enforcement to guidance language"
slug: file-scope-advisory
file_scope:
  - src/theforge/task.py
  - tests/test_task.py
pytest_target: tests/
---

# file_scope Advisory Mode

## Problem

`file_scope` enforcement in `build_dev_prompt()` is too rigid. The current
language says "You may ONLY create or modify files in these locations" and
includes the `SCOPE_BLOCKED` hard-stop instruction. This causes problems:

- Spec authors can't always predict every file needed
- The review process already catches unauthorized changes
- Reviewers flag unexpected out-of-scope edits as P1/P2 anyway

## Solution

Change `build_dev_prompt()` file_scope section from hard enforcement to
advisory guidance. One file, one function, minimal change.

### Current language (lines ~274-293 in task.py):

```
You may ONLY create or modify files in these locations:
{file_scope_str}

**If you determine that correctly implementing this spec requires modifying
a file NOT in the list above:**
1. Do NOT implement a workaround within in-scope files.
2. Do NOT commit any code changes.
3. Output ONLY the following...
SCOPE_BLOCKED: ...
```

### Replace with:

```
Focus your changes on these files:
{file_scope_str}

If you need to touch a file not listed here, do so — but keep changes
minimal and directly related to the spec. The reviewer will flag any
unexpected out-of-scope changes.
```

Remove the entire `scope_blocked_block` variable, the `{scope_blocked_block}`
interpolation, and the SCOPE_BLOCKED instruction paragraphs from
`build_dev_prompt()`. The `_parse_scope_blocked()` function and coordinator
detection can remain (they do no harm), but must not be referenced in the
dev prompt.

Do NOT change `build_fix_prompt()` or any other function.

## Acceptance Criteria

- [ ] `build_dev_prompt()` file_scope section uses "Focus your changes on"
      language instead of "ONLY create or modify"
- [ ] `scope_blocked_block` variable removed from `build_dev_prompt()`
- [ ] `SCOPE_BLOCKED` sentinel instruction removed from `build_dev_prompt()`
- [ ] `build_fix_prompt()` unchanged
- [ ] All other functions in task.py unchanged
- [ ] Existing tests pass without modification
- [ ] New test: `build_dev_prompt()` with non-empty file_scope contains
      "Focus your changes" and does NOT contain "SCOPE_BLOCKED"
- [ ] New test: `build_dev_prompt()` with empty file_scope still works correctly
