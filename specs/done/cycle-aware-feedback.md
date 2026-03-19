---
name: "Cycle-aware dev feedback (anti-churn)"
slug: cycle-aware-feedback
pytest_target: tests/
---

# Cycle-Aware Dev Feedback

## Problem

Review cycles churn because the dev agent has no memory across iterations.
`state.last_review_findings` is overwritten each cycle — on iteration 3, dev
only sees cycle 3's findings with zero visibility into what cycles 1 and 2
flagged or what fixes were already attempted.

The result: dev re-attempts the same broken fix, reviewer flags the same issue,
coordinator escalates the model silently, and nobody learns anything. Three
cycles burn budget producing the same outcome.

In a real code review, the reviewer says "you tried X last round and it didn't
work — try Y instead." The dev sees the full thread, not just the latest comment.

Additionally, when `_has_persistent_p1` triggers a model escalation, the dev
agent is never told. It gets a stronger model but no context about why — so
it still doesn't know what was already tried.

## Requirements

1. The coordinator accumulates a cycle history: for each completed review cycle,
   store the cycle number, verdict, summary, and P1 findings
2. On iteration 2+, the dev agent receives the accumulated history before the
   current findings — "Cycle 1 flagged X, Cycle 2 still flagged Y"
3. When model escalation fires (persistent P1), inject an explicit note into
   the dev prompt: which finding persisted, what model was used before, what
   model is being used now
4. The fix prompt (`build_fix_prompt`) includes the cycle history section
5. Cycle history is capped — only the last N cycles are included (avoid prompt
   bloat on long-running tasks)

## Acceptance Criteria

- [ ] `CoordinatorState` accumulates review cycle summaries (not just raw
      `ReviewResult` objects — a lightweight history)
- [ ] `build_fix_prompt` includes a "Previous Cycles" section showing what
      was flagged and attempted in earlier cycles
- [ ] When no previous cycles exist (first review), no history section appears
- [ ] When model escalation fires, the dev prompt includes an explicit note
      about the escalation and which finding triggered it
- [ ] Cycle history is capped at 3 most recent cycles to bound prompt size
- [ ] `review_to_dev_handoff` output is unchanged (reviewer→dev format stays)
- [ ] All existing tests pass
