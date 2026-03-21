---
name: "ESCALATE → HITL gate: allow human to promote to APPROVE"
slug: escalate-hitl
pytest_target: tests/
---

# ESCALATE → HITL Gate

## Problem

When a sprint escalates (max review cycles, budget overrun, or infrastructure
failure), the coordinator exits with `outcome: escalate`. No PR is created,
no downstream automation fires (issue filing works via hook, but PR creation
and auto-merge don't).

This forces the human to manually:
1. Read the handoff to understand what happened
2. Decide if the code is actually good enough (e.g. 3/4 reviewers APPROVE,
   one failed for infrastructure reasons)
3. Manually create a PR or merge the branch
4. Manually close milestone issues

In practice, many escalations are not code quality failures — they're
infrastructure failures (DeepSeek budget blowout, API timeouts, parse
failures). The human would approve if given the chance.

## Solution

Add a HITL decision gate at the ESCALATE exit point. When the coordinator
would normally exit with `outcome: escalate`, it instead pauses and presents
the human with a decision:

- **approve** — treat as APPROVE. Create PR (or auto-merge per config),
  fire post_run hook, close milestone issues. Full downstream automation.
- **reject** — exit as escalate (current behavior). Worktree preserved.
- **resume** — re-enter the coordinator loop at the current phase for
  another iteration (e.g. one more review cycle with different parameters).

### Interactive mode (terminal)

```
[forge] ⚠ ESCALATE — max review cycles reached
[forge]   Reviewers: claude(APPROVE) codex(APPROVE) gemini(APPROVE) deepseek(FAIL)
[forge]   Reason: deepseek-reviewer budget exceeded ($1.02 > $1.00)
[forge]   Gate: PASS (1035 tests)
[forge]
[forge]   Choose:
[forge]     [a] Approve — treat as APPROVE, create PR / merge
[forge]     [r] Reject  — exit as ESCALATE, preserve worktree
[forge]     [c] Continue — run one more review cycle
[forge]   Choice:
```

### Remote mode (ntfy)

When `--notify` is active, send an ntfy notification with action buttons:

```
Title: TheForge: ESCALATE — review-convergence
Body:  3/4 reviewers APPROVE, deepseek FAIL (budget exceeded)
       Gate: PASS (1035 tests)

Actions: Approve, Reject, Continue
```

Long-poll for reply, same pattern as remote-hitl.

### Non-interactive mode

When running non-interactively (no terminal, no ntfy), check for an
`escalate_policy` config in forge.yaml:

```yaml
retry:
  escalate_policy: prompt   # prompt (default) | auto_approve | reject
```

- `prompt` — wait for human input (blocks if no interaction method available)
- `auto_approve` — if gate passed and majority of reviewers approved, treat
  as APPROVE automatically
- `reject` — current behavior, exit as escalate

### Decision context

The HITL prompt must include enough context for the human to decide:
- Per-reviewer verdicts (which approved, which failed, why)
- Gate result (PASS/FAIL, test count)
- Total cost spent
- Escalation reason (max cycles, budget, timeout, infrastructure)
- Number of dev iterations completed

## Coordinator changes

### New escalation flow

```
Current:  REVIEW → (max cycles) → ESCALATE → exit
Proposed: REVIEW → (max cycles) → ESCALATE_GATE → human decision
                                    ├─ approve → on_approve flow (PR/merge + hook)
                                    ├─ reject  → exit as ESCALATE
                                    └─ continue → back to REVIEW
```

### `_run_escalate_gate()` function

New function in `coord_phases.py`. Receives `CoordinatorState`, builds
the decision context from state fields, presents to human via terminal
or ntfy, returns decision.

### State additions

```python
escalate_decision: str | None = None  # "approve" | "reject" | "continue"
escalate_reason: str | None = None    # human-readable escalation reason
```

### Audit additions

```yaml
escalation:
  reason: "max review cycles reached"
  reviewer_verdicts:
    claude-reviewer: APPROVE
    codex-reviewer: APPROVE
    gemini-reviewer: APPROVE
    deepseek-reviewer: FAIL (budget exceeded)
  gate_result: PASS
  human_decision: approve
  waited_seconds: 12.3
```

## Acceptance Criteria

- [ ] ESCALATE triggers HITL gate instead of immediate exit
- [ ] Interactive mode: decision prompt with context (per-reviewer verdicts,
      gate result, cost, escalation reason)
- [ ] Remote mode: ntfy notification with Approve/Reject/Continue actions
- [ ] `escalate_policy: auto_approve` auto-approves when gate passed and
      majority approved
- [ ] `escalate_policy: reject` preserves current behavior
- [ ] On approve: full on_approve flow fires (PR/merge + hook)
- [ ] On continue: coordinator re-enters REVIEW for one more cycle
- [ ] On reject: exits as ESCALATE (current behavior)
- [ ] `escalate_decision` and `escalate_reason` in CoordinatorState
- [ ] Audit YAML includes escalation section with decision context
- [ ] All existing tests pass
- [ ] New tests: approve path, reject path, continue path, auto_approve policy,
      non-interactive reject default
