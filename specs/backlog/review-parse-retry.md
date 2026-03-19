---
name: "Review parse errors: retry reviewer instead of consuming cycle"
slug: review-parse-retry
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/review.py
  - tests/test_coordinator.py
pytest_target: tests/test_coordinator.py
---

# Review Parse Retry

## Problem

When a reviewer returns malformed YAML (parse error, wrong root type,
missing required fields), the coordinator currently treats it as
`REQUEST_CHANGES` and increments the review cycle counter. This is wrong:

- A parse error is a **transient AI reliability failure**, not a code quality verdict
- The code was never evaluated — the reviewer simply failed to produce output
- Counting it as a cycle wastes the retry budget (max_review_cycles)
- After N cycles of parse errors, the task escalates as if code was bad
- The human sees "Review requested changes after N cycles" with no real findings

**Observed in the wild:** `reasoning-effort` spec escalated after 2 cycles.
Cycle 1 was a pure parse error (`Review output root is not a YAML mapping`).
Cycle 2 found a real P1. The parse error consumed half the review budget.

## Context

### Current behavior

In `coordinator.py`, when `run_agent_pool()` returns a result and parsing
fails, `_synthesize_or_single()` (or equivalent) returns a verdict of
`REQUEST_CHANGES` with summary `"PARSE ERROR: ..."`. The coordinator
treats this identically to a real `REQUEST_CHANGES`, increments
`state.review_cycle`, and loops back to DEV.

### Review failure classification

There are three distinct failure modes:

| Type | What happened | Correct response |
|------|--------------|-----------------|
| Parse error | Reviewer returned invalid YAML | Retry the reviewer (up to N times) |
| Schema error | Valid YAML but invalid structure | Retry the reviewer once (may be fixable) |
| `REQUEST_CHANGES` with P1 | Real finding | Increment cycle, loop to DEV |
| All reviewers failed | No successful review | Escalate with "review unreliable" |

## Design

### Per-reviewer retry on parse/schema error

Add a `max_review_parse_retries` policy (default: 2) to `RetryPolicy`:

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_dev_iterations: int = 3
    max_review_cycles: int = 2
    max_review_parse_retries: int = 2  # per reviewer, per cycle
```

In the review execution loop, track parse/schema failures per reviewer.
If a reviewer fails to parse, retry it up to `max_review_parse_retries`
times **without** incrementing `state.review_cycle`. Only increment the
cycle counter when a reviewer successfully produces a parseable verdict
(`APPROVE` or `REQUEST_CHANGES` with findings).

### Parse error detection

A parse error is any result where `_parse_review_output()` raises or
returns a result whose `summary` starts with `"PARSE ERROR"` or
`"SCHEMA ERROR"`. These strings are already produced by the existing
review parsing code — no new classification needed.

### Failure escalation

If all retries for all reviewers in a pool are exhausted without a
single parseable verdict, escalate with:

```
"Review pool unreliable: all reviewers failed to produce valid output
after N retries. Last error: {last_error}"
```

This is a distinct escalation reason from "max cycles exhausted" —
it tells the human what actually went wrong.

### Review cycle semantics (unchanged)

A review cycle still means: dev agent ran, gate passed, review pool ran,
synthesized verdict was `REQUEST_CHANGES` → loop back to DEV. Parse
retries are invisible to the cycle counter. Audit log should record
parse retries separately:

```yaml
reviews:
  - cycle: 1
    verdict: REQUEST_CHANGES
    parse_retries: 1        # NEW: how many parse retries happened this cycle
    p1_count: 1
    ...
```

## Acceptance Criteria

1. A parse error on a reviewer does NOT increment `state.review_cycle`
2. The reviewer is retried up to `max_review_parse_retries` times
3. After successful retry, normal review flow continues
4. If all retries exhausted with no parseable verdict → escalate with
   "review pool unreliable" message (not "max cycles exhausted")
5. `state.review_cycle` only increments on a real `REQUEST_CHANGES` verdict
6. Audit log records `parse_retries` per cycle
7. `max_review_parse_retries` is configurable in forge.yaml under `retry:`
8. Default `max_review_parse_retries: 2` preserves existing behavior for
   projects that don't set it

## Test Expectations

In `tests/test_coordinator.py`:

- `test_parse_error_does_not_increment_cycle` — first review attempt
  returns parse error, second attempt returns APPROVE → result is DONE,
  `state.review_cycle == 1` (not 2)
- `test_parse_error_then_request_changes` — parse error then real
  REQUEST_CHANGES → cycle increments once, DEV retried
- `test_all_parse_retries_exhausted` — all N parse retries fail →
  phase=ESCALATE, message contains "unreliable"
- `test_parse_retry_count_in_audit` — audit log shows `parse_retries: 1`
  when one retry occurred
- `test_schema_error_also_retried` — schema validation error (not just
  YAML parse error) also triggers retry, not cycle increment

## Out of Scope

- Retrying a reviewer that returns a valid `REQUEST_CHANGES` with findings
  (that's a real verdict, not a transient failure)
- Changing synthesis behavior for parse errors in multi-model pools
  (synthesis only runs when all reviewers produce valid output)
