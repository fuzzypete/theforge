---
name: "Gate hardening: dirty-worktree fix and infrastructure error diagnostics"
slug: gate-hardening
file_scope:
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/test_coordinator.py
---

# Gate Hardening

## Problem

Two regressions introduced with the exit-code gate mode:

### P1: Dirty-worktree check bypassed in exit-code mode

When `handoff_file` is `""`, the clean-worktree guard uses
`endswith(config.validation.handoff_file)` which becomes `endswith("")` —
true for every path. This means **all files are filtered out**, the worktree
always appears clean, and the coordinator can return `DONE` (and even
auto-merge) while uncommitted changes are sitting in the worktree.

### P2: Infrastructure failures collapsed into gate FAIL

`_run_shell()` returns `ok=False` for both normal test failures AND
infrastructure problems (timeout, command not found, shell error).
Exit-code mode maps every `ok=False` to `"FAIL"`, so a
misconfigured `gate_command` or too-short `gate_timeout` burns dev
retries and escalates as "Gate returned FAIL" — hiding the real cause.

## Context

### Relevant code

```python
# coordinator.py — dirty worktree check (broken in exit-code mode)
dirty = [
    line for line in output.splitlines()
    if not line.endswith(config.validation.handoff_file)  # "" matches everything
]

# coordinator.py — exit-code gate branch
if use_exit_code:
    if ok:
        return "PASS", None
    _log(f"Gate command failed: {output[:200]}")
    return "FAIL", None  # no distinction between test failure vs infra error
```

`_run_shell()` returns a special sentinel output for timeout/error:
check for `"TIMEOUT"` or `"ERROR"` prefixes in the output string, or
check the existing timeout/error handling in `_run_shell`.

## Design

### Fix 1: Dirty-worktree check for exit-code mode

In the worktree clean check, when `handoff_file` is empty, skip the
`endswith` filter entirely — no file should be excluded:

```python
if config.validation.handoff_file:
    dirty = [
        line for line in output.splitlines()
        if line and not line.endswith(config.validation.handoff_file)
    ]
else:
    dirty = [line for line in output.splitlines() if line]
```

### Fix 2: Infrastructure error diagnostics

In `_run_gate()`, inspect the output for known `_run_shell` error
sentinels before returning `"FAIL"`. If the gate failed due to
infrastructure (timeout, command not found), return an error string
instead of `"FAIL"` so the coordinator logs it clearly and doesn't
retry as if it were a code quality failure:

```python
if use_exit_code:
    if ok:
        return "PASS", None
    # Distinguish infrastructure failure from ordinary test failure
    if "TIMEOUT" in output or "timed out" in output.lower():
        return None, f"Gate timed out (gate_timeout={config.validation.gate_timeout}s). Consider increasing gate_timeout."
    if output.startswith("ERROR:"):
        return None, f"Gate infrastructure error: {output[:300]}"
    _log(f"Gate command failed (exit non-zero): {output[:200]}")
    return "FAIL", None
```

Check `_run_shell` to confirm the exact sentinel strings used for
timeout and error conditions, and match them precisely.

## Acceptance Criteria

1. In exit-code mode, dirty worktree files are correctly detected (no
   false-clean results when `handoff_file` is `""`)
2. In handoff mode, dirty worktree behavior is unchanged
3. Gate timeout in exit-code mode returns an error (not `"FAIL"`) with
   a message suggesting to increase `gate_timeout`
4. Infrastructure errors in exit-code mode return an error (not `"FAIL"`)
5. Normal test failures in exit-code mode still return `"FAIL"` (retried)
6. Handoff-based gate error handling is unchanged

## Test Expectations

In `tests/test_coordinator.py`:

- `test_exit_code_dirty_worktree_detected` — exit-code mode + dirty
  files in git status → coordinator does NOT return DONE, dirty files
  are reported
- `test_handoff_dirty_worktree_unchanged` — handoff mode dirty check
  still filters out the handoff file correctly (regression guard)
- `test_exit_code_gate_timeout_is_error` — `_run_shell` returns timeout
  sentinel → gate returns `(None, error_msg)` containing "timed out" or
  "gate_timeout", coordinator escalates with clear message not FAIL retry
- `test_exit_code_infrastructure_error_is_error` — `_run_shell` returns
  error sentinel → gate returns `(None, error_msg)`, not `"FAIL"`
- `test_exit_code_test_failure_is_fail` — normal non-zero exit (tests
  failing) → gate returns `("FAIL", None)`, retried as normal
