---
name: "Fix persistent P1 detection for smart model escalation"
slug: persistent-p1-detection
file_scope:
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Fix Persistent P1 Detection

## Problem

`smart_config_models` is supposed to escalate the dev model from sonnet →
codex → opus when a P1 persists across consecutive review cycles. In
practice it never fires because `_has_persistent_p1()` requires
`curr.file == prev.file` before comparing descriptions.

Two reasons this condition is never met:

1. **Reviewers rephrase the same problem** — codex might say "coordinator's
   routing ignores the extend path" in cycle 1 and "extend resets session ID
   making fix-prompt fire incorrectly" in cycle 2. Same issue, different
   description, different enough to fail the 60% token overlap check.

2. **Reviewers flag different files** — the same underlying bug touches
   coordinator.py in one cycle and task.py in the next depending on which
   reviewer surfaces it. File mismatch → no match → no escalation.

Result: sonnet runs all 5 cycles at full cost. Codex and opus never engage.
The smart_config_models feature is effectively dead.

## Root Cause

```python
def _has_persistent_p1(current_findings, previous_findings):
    for curr in current_p1s:
        for prev in previous_p1s:
            if curr.file != prev.file:   # ← too strict
                continue
            # description similarity check ...
```

The file match gate eliminates all cross-file comparisons before the
description check even runs.

## Solution

Remove the `curr.file != prev.file` guard. Match on description similarity
alone across all P1 pairs.

```python
def _has_persistent_p1(current_findings, previous_findings):
    current_p1s = [f for f in current_findings if f.severity == "P1"]
    previous_p1s = [f for f in previous_findings if f.severity == "P1"]

    if not current_p1s or not previous_p1s:
        return False

    for curr in current_p1s:
        for prev in previous_p1s:
            # Substring containment
            if curr.description in prev.description or prev.description in curr.description:
                return True
            # Token overlap ≥ 60%
            curr_tokens = set(curr.description.lower().split())
            prev_tokens = set(prev.description.lower().split())
            if not curr_tokens or not prev_tokens:
                continue
            overlap = len(curr_tokens & prev_tokens) / max(len(curr_tokens), len(prev_tokens))
            if overlap >= 0.6:
                return True

    return False
```

No other changes needed — the escalation logic that follows already handles
the model switch correctly once `_is_persistent_p1` returns True.

## Acceptance Criteria

- [ ] `_has_persistent_p1()` removes the `curr.file == prev.file` guard
- [ ] Matching is description-only (substring containment OR ≥60% token overlap)
- [ ] Returns True when same P1 appears across cycles regardless of file
- [ ] Returns False when P1s are genuinely different issues
- [ ] Returns False if either cycle has no P1s
- [ ] Existing tests pass without modification
- [ ] New test: same P1 description, different files → returns True
- [ ] New test: same P1 description, same files → still returns True
- [ ] New test: different P1 descriptions → returns False
- [ ] New test: empty findings in either cycle → returns False
