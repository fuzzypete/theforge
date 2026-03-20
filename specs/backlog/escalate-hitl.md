---
name: "ESCALATE → HITL gate: allow human to promote to APPROVE"
slug: escalate-hitl
pytest_target: tests/
---

# ESCALATE → HITL Gate

## Problem

When a sprint escalates (budget exceeded, max review cycles hit, or
infrastructure failure), the run ends immediately. No PR is created,
no issues are filed via the on_approve path, and the human must manually
reconstruct the outcome — creating PRs, reviewing code, filing issues
by hand.

In practice, many escalations are approvable: 3/4 reviewers APPROVE but
one fails for infrastructure reasons (DeepSeek API error, iteration
churn). The code is fine. The human would say "approve" if asked.

## Solution

Add a HITL decision gate at the ESCALATE transition. Before terminating,
the coordinator pauses and presents the human with context and options:

### Decision prompt

```
Sprint escalated: <reason>

Context:
  - Review cycles completed: N
  - Last review verdict: <verdict> (P1: X, P2: Y)
  - Reviewers: <list with per-reviewer verdicts>
  - Gate result: <PASS/FAIL>
  - Total cost: $X.XX

Options:
  [a] Approve — create PR, file issues, mark done
  [r] Resume  — continue from current phase
  [x] Escalate — terminate as today (leave worktree intact)

Choice:
```

### Behavior

- **Approve**: Coordinator treats the run as DONE/APPROVE. Creates PR
  (if on_approve: pr), fires post_run hook, files issues. Full
  downstream automation runs.
- **Resume**: Re-enter the state machine at the current phase. Useful
  when the escalation was transient (API outage resolved, budget
  increased).
- **Escalate**: Current behavior — terminate, leave worktree, log
  escalation.

### Interactive vs remote

- **Interactive (terminal)**: Print decision prompt, read stdin.
- **Remote (ntfy)**: Send notification with context and action buttons.
  Same pattern as existing remote-hitl for HUMAN_REVIEW.

### Audit

```yaml
escalation_gate:
  reason: "max review cycles (3)"
  decision: approve    # approve | resume | escalate
  waited_seconds: 12.5
  context:
    review_cycles: 3
    last_verdict: APPROVE
    p1_count: 0
    p2_count: 5
    gate_result: PASS
    cost_usd: 7.24
```

## Acceptance Criteria

- [ ] Coordinator pauses at ESCALATE transition with decision prompt
- [ ] Decision prompt shows review context (cycles, verdicts, costs)
- [ ] Approve option triggers full DONE path (PR, hook, issues)
- [ ] Resume option re-enters state machine at current phase
- [ ] Escalate option preserves current behavior
- [ ] Interactive mode: terminal prompt with stdin
- [ ] Remote mode: ntfy notification with action buttons
- [ ] Audit log captures escalation gate decision and context
- [ ] All existing tests pass
- [ ] New tests: approve path, resume path, escalate path
