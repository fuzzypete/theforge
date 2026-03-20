---
name: "Schema-enforce line numbers on P1 code review findings"
slug: p1-line-enforcement
pytest_target: tests/
---

# Schema-Enforce Line Numbers on P1 Code Review Findings

Ref: GitHub Issue #3

## Problem

The review schema in `schemas.py` allows `line: null` on any finding. For P1
findings, a null line number makes the finding much harder for the dev agent to
act on and harder to de-duplicate across reviewers in pool merges.

## Solution

Add a cross-validation rule in `schemas.py`: P1 findings where `file` is set
MUST have a non-null `line`. Null line is only acceptable on P1s where `file`
is also null (e.g., an architectural finding with no specific location).

This is consistent with existing cross-validation rules (APPROVE+P1 and
REQUEST_CHANGES+no-P1 are already errors).

## Files

- `src/theforge/schemas.py` — add cross-validation rule
- `tests/test_schemas.py` — add tests for the new rule

## Acceptance criteria

- [ ] P1 finding with `file` set and `line: null` → schema validation error
- [ ] P1 finding with `file: null` and `line: null` → valid (architectural finding)
- [ ] P1 finding with `file` set and `line` set → valid (normal case)
- [ ] P2 findings are NOT affected by this rule (line remains optional)
- [ ] New test: P1 + file + null line → error
- [ ] New test: P1 + null file + null line → no error
- [ ] New test: P2 + file + null line → no error
- [ ] Existing tests pass unchanged
