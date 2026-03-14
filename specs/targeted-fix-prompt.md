---
name: "Targeted fix prompt for review iterations"
slug: targeted-fix-prompt
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/task.py
  - tests/test_task.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Targeted Fix Prompt for Review Iterations

## Problem

When a review returns REQUEST_CHANGES with P1 findings, the coordinator loops
back to DEV and rebuilds the **full original prompt** via `build_dev_prompt()`:
full spec, full preflight output, full 9-step implementation instructions, plus
the review findings appended at the end.

The dev agent treats iteration 2 like a fresh task: re-reads the spec, re-orients
on the codebase, re-runs `make fmt`, `make lint`, and the full gate — all for a
targeted P1 fix. Observed cost: iteration 2 burns as much time and tokens as
iteration 1 ($4.67 / 15 min vs $4.25 / 17 min for the same task).

The session IS resumed (`session_id` is carried forward), so the agent already has
its full conversation history. Sending the full spec + preflight again is pure
waste — the agent already read it all on iteration 1.

## Root Cause

The coordinator has **no explicit routing state** to distinguish why it's looping
back to DEV. There are 6 distinct re-entry paths into the DEV phase, and each
needs different prompt behavior:

| # | Path | State after transition | Desired prompt |
|---|------|----------------------|----------------|
| 1 | First run (fresh) | `retry_reason=None` | `build_dev_prompt()` |
| 2 | Review REQUEST_CHANGES | `retry_reason="review_changes"`, `last_review_findings` set | **`build_fix_prompt()`** |
| 3 | Gate failure (exit-code mode) | `retry_reason="gate_fail"`, `human_feedback` set | `build_dev_prompt()` |
| 4 | Dirty worktree | `retry_reason="dirty_worktree"`, `human_feedback` set | `build_dev_prompt()` |
| 5 | Gate FAIL/BLOCKED (handoff mode) | `retry_reason="gate_fail"`, `human_feedback` set | `build_dev_prompt()` |
| 6 | Human `extend` | `retry_reason="extend"`, `last_review_findings` set | **`build_fix_prompt()`** |
| 7 | Human `reject` | `retry_reason="reject"`, `human_feedback` set | `build_dev_prompt()` |

Without `retry_reason`, paths 2 and 6 are indistinguishable from paths 3/4/5
using only `dev_iteration`, `last_review_findings`, and `human_feedback` — the
agent cannot converge on a correct routing condition.

## Solution

### 1. Add `retry_reason` to `CoordinatorState`

```python
@dataclass
class CoordinatorState:
    ...
    retry_reason: str | None = None  # "review_changes" | "gate_fail" | "dirty_worktree" | "extend" | "reject" | None
```

### 2. Set `retry_reason` at each retry site in coordinator.py

These are the exact locations where `continue` loops back to DEV:

**Review REQUEST_CHANGES** (~line 1912):
```python
state.last_review_findings = findings_to_markdown(parsed_review.findings)
state.dev_iteration = 0
state.human_feedback = None
state.retry_reason = "review_changes"  # ← ADD
```

**Gate failure — exit-code mode** (~line 1333):
```python
state.human_feedback = f"Gate validation failed: {gate_err}"
state.retry_reason = "gate_fail"  # ← ADD
```

**Dirty worktree** (~line 1374):
```python
state.human_feedback = "PROCESS VIOLATION: ..."
state.retry_reason = "dirty_worktree"  # ← ADD
```

**Gate FAIL/BLOCKED — handoff mode** (~line 1396):
```python
state.human_feedback = f"Gate returned {gate_decision}. ..."
state.retry_reason = "gate_fail"  # ← ADD
```

**Human `extend`** (~line 1721):
```python
state.last_review_findings = findings_to_markdown(parsed_review.findings)
state.human_feedback = None
state.retry_reason = "extend"  # ← ADD
```

**Human `reject`** (~line 1732):
```python
state.human_feedback = feedback
state.last_review_findings = None
state.retry_reason = "reject"  # ← ADD
```

There is a second `extend` path (~line 1874) and a second `reject` path (~line
1887) in the cycles-exhausted branch. Set `retry_reason` at those too.

### 3. Add `build_fix_prompt()` to task.py

```python
def build_fix_prompt(
    task: TaskSpec,
    *,
    workspace_path: Path,
    branch_name: str,
    review_findings: str,
    gate_command: str,
    gate_skipped: bool = False,
    iteration: int = 2,
) -> str:
```

The prompt contains ONLY:
- Working directory and branch (agent needs these)
- The P1/P2 findings with file paths, line numbers, and suggestions
- Instruction to fix the findings, run `make fmt`, and commit
- Note that the coordinator will re-run the gate (agent should NOT run it)
- If `gate_skipped` is True, omit the gate note entirely

No spec recap. No preflight output. No "implementation steps 1-9." The agent
already has full context from iteration 1's resumed session.

### 4. Route in coordinator DEV phase

At the top of the DEV phase, BEFORE calling `build_dev_prompt()`:

```python
if state.retry_reason in ("review_changes", "extend") and state.last_review_findings:
    prompt = build_fix_prompt(
        task,
        workspace_path=workspace_path,
        branch_name=branch_name,
        review_findings=state.last_review_findings,
        gate_command=config.validation.gate_command,
        gate_skipped=_is_gate_skip(task.gate_override),
        iteration=state.dev_iteration,
    )
else:
    prompt = build_dev_prompt(...)  # existing call, unchanged
```

Clear `retry_reason` after using it (so it doesn't leak to the next iteration):
```python
state.retry_reason = None  # consumed
```

### 5. Do NOT modify `build_dev_prompt()`

`build_dev_prompt()` remains unchanged. It still handles: first run, gate
failures, dirty worktree retries, and human reject — all cases that need the
full context.

## Acceptance Criteria

- [ ] `retry_reason` field added to `CoordinatorState` (default `None`)
- [ ] `retry_reason` set at all 8 retry sites (6 listed above + 2 duplicates
      in cycles-exhausted branch)
- [ ] `build_fix_prompt()` exists in `task.py`
- [ ] Fix prompt contains ONLY: workspace info, findings, minimal instructions
- [ ] Fix prompt does NOT contain: spec content, preflight output, full
      implementation steps
- [ ] Fix prompt respects `gate_skipped` (omits gate note when True)
- [ ] Coordinator routes to `build_fix_prompt()` when `retry_reason` is
      `"review_changes"` or `"extend"` AND `last_review_findings` is set
- [ ] Coordinator routes to `build_dev_prompt()` for all other cases
- [ ] `retry_reason` is cleared after prompt is built (set to `None`)
- [ ] Existing tests pass without modification
- [ ] New test: fix prompt content is minimal (no spec, no preflight)
- [ ] New test: coordinator routes to fix prompt on `retry_reason="review_changes"`
- [ ] New test: coordinator routes to dev prompt on `retry_reason="gate_fail"`
- [ ] New test: coordinator routes to fix prompt on `retry_reason="extend"`
