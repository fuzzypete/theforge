---
name: "Review pool resilience — per-reviewer parse retry + graceful empty-pool handling"
slug: review-pool-resilience
pytest_target: tests/
---

# Review Pool Resilience

## Problem

Cycle 2 of the `lifecycle-hooks` run escalated despite all 4 reviewers
"succeeding". The audit shows `parse_retries: 1`, all reviewers in
`successful`, but no `verdict` in the cycle 2 record. The coordinator hit
`max_review_cycles` and escalated — not because the code was bad, but because
the review merge produced empty output and the failure was swallowed silently.

Two root causes:

**1. Per-reviewer parse retry is pool-level, not reviewer-level.**
`parse_retries` tracks total retries across the pool. When one reviewer's
output fails schema validation, the entire pool is retried (all reviewers
re-run). This is expensive and doesn't isolate the bad reviewer.

**2. Empty merge result escalates instead of degrading.**
When `_merge_reviews()` produces a result with no verdict (e.g. all individual
outputs failed schema cross-validation during merge), the coordinator records a
cycle with no verdict and proceeds to the next cycle. At `max_review_cycles` it
escalates. There is no warning, no degradation, no "use what we have".

## Solution

**Per-reviewer parse retry:** After each reviewer finishes, validate its output
immediately. If schema validation fails, retry that single reviewer (up to
`max_review_parse_retries`) before marking it failed and excluding it. Other
reviewers are unaffected.

**Graceful empty-pool handling:** If `_merge_reviews()` produces no verdict
(empty findings, null verdict, or merge validation error), do not silently
record an empty cycle. Instead:
1. Log a clear warning: which reviewers contributed, what the merge produced
2. Fall back to the highest-severity individual result if any reviewer produced
   a valid parsed output
3. If truly nothing usable, treat the cycle as REQUEST_CHANGES (fail-safe) with
   a synthetic finding explaining the pool failure — never silently escalate

## Changes Required

### `src/theforge/coord_phases.py`

**Per-reviewer retry loop** — in `_run_review_phase()`, after invoking each
reviewer, validate its parsed output immediately:

```python
for profile in pool_profiles:
    for attempt in range(max_review_parse_retries + 1):
        result = _invoke_reviewer(profile, ...)
        parsed = _try_parse_review(result.output)
        if parsed is not None:
            break
        if attempt < max_review_parse_retries:
            _log(f"  ↻ {profile.name} parse failed, retry {attempt+1}")
    if parsed is None:
        failed_reviewers.append(profile.name)
    else:
        successful_results.append((profile.name, parsed))
```

**Graceful empty merge** — after `_merge_reviews()`:

```python
merged = _merge_reviews(successful_results)
if merged.verdict is None:
    _log(f"  ⚠ review merge produced no verdict — falling back to best individual result")
    merged = _best_individual_result(successful_results)
if merged.verdict is None:
    # truly nothing usable — fail safe
    merged = ParsedReview(
        verdict="REQUEST_CHANGES",
        summary="Review pool failed to produce a usable verdict",
        findings=[Finding(
            severity="P1",
            file=None, line=None,
            description="All reviewers failed to produce parseable output. Manual review required.",
            suggestion="Check reviewer logs for details."
        )],
        ...
    )
    _log(f"  ⚠ all reviewers failed — synthetic P1 injected, returning REQUEST_CHANGES")
```

### `src/theforge/review.py`

- `_best_individual_result(results)` — returns the ParsedReview with the
  highest severity finding, or the first APPROVE if all reviewers approved
- Add `_try_parse_review(output) -> ParsedReview | None` — wraps existing
  parse logic, returns None on any error rather than raising

### Tests

- Per-reviewer retry fires on single-reviewer schema failure, other reviewers unaffected
- `max_review_parse_retries=0` disables per-reviewer retry
- Empty merge falls back to best individual result
- Best individual result is REQUEST_CHANGES when any reviewer had P1
- Synthetic P1 injected when truly nothing usable
- All existing review pool tests continue to pass

## Acceptance Criteria

- [ ] Per-reviewer parse failure triggers retry of that reviewer only (up to
      `max_review_parse_retries`), not the entire pool
- [ ] Other reviewers in the pool are unaffected by one reviewer's parse failure
- [ ] `_merge_reviews()` returning no verdict falls back to best individual result
- [ ] Best individual result: P1 from any reviewer → REQUEST_CHANGES; all APPROVE → APPROVE
- [ ] If no individual results usable: synthetic P1 injected, REQUEST_CHANGES returned
- [ ] Clear warning logged in all fallback cases
- [ ] No silent escalation due to empty review merge
- [ ] All existing tests pass
