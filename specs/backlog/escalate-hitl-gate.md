---
name: "ESCALATE HITL gate — human decision before terminal failure"
slug: escalate-hitl-gate
pytest_target: tests/
---

# ESCALATE HITL gate — human decision before terminal failure

## Problem

When the coordinator reaches ESCALATE (budget overrun, max review cycles, max dev
iterations), it currently terminates immediately. The post_run hook fires with
verdict=ESCALATE, but no PR is created, no issues are filed for findings, and the
human gets no opportunity to override.

Real-world pain: a sprint had 3/4 reviewers APPROVE (claude, codex, gemini) but
DeepSeek hit its $1 budget ceiling after 50 iterations of churn — a behavioral bug,
not a code problem. The coordinator escalated, killing the entire downstream
automation (PR creation, hook-driven GH issue filing). The human would have approved
instantly.

## Solution

Add an ESCALATE_REVIEW phase. When the coordinator would enter ESCALATE, it instead
pauses and surfaces a HITL decision gate.

### Decision options

- **approve** — treat as APPROVE. Create PR, fire hooks, full downstream automation.
- **reject** — terminate. Worktree preserved, no PR, no merge.
- **resume** — bump budget/iteration limits and continue from the phase that failed.
  (e.g., add $2 to deepseek budget and re-enter REVIEW)

### Context surfaced at the gate

- Why escalation happened (budget exceeded, max cycles, etc.)
- Per-reviewer verdicts from the last review cycle
- P1/P2 finding summary
- Total cost so far
- Suggestion based on available evidence (e.g., "3/4 reviewers approved, 1 failed
  due to budget — consider approving")

### Modes

- **Interactive (terminal)**: Print context, prompt for decision
- **Remote (ntfy)**: Push notification with decision summary + action buttons
  (Approve/Reject/Resume), long-poll for response
- **Headless (CI)**: Auto-reject (preserve current behavior for unattended runs)

### Coordinator changes

- New `Phase.ESCALATE_REVIEW` in the state machine
- `_run_escalate_review()` returns decision enum
- On approve: transition to DONE, run the on_approve flow (PR creation, hook)
- On reject: transition to ESCALATE (current terminal behavior)
- On resume: adjust limits in config, re-enter the failing phase
- `CoordinatorState` gains `escalate_decision`, `escalate_reason`,
  `escalate_context`

### Config

```yaml
escalate_review:
  enabled: true          # default: true (opt-out, not opt-in)
  mode: blocking         # blocking | headless
  timeout_seconds: 14400 # 4h for remote/ntfy mode
```

Default enabled because the whole point is to prevent the current failure mode.

### Audit

```yaml
escalate_review:
  reason: "Review budget exceeded for deepseek-reviewer: spent $1.08 (limit $1.00)"
  decision: approve
  waited_seconds: 12.3
  context_summary: "3/4 reviewers APPROVE, 1 FAIL (budget). 0 P1, 4 P2."
```

## Acceptance criteria

- Phase.ESCALATE_REVIEW added to state machine
- Coordinator pauses before ESCALATE when escalate_review.enabled is true
- Interactive mode: prints escalation context, prompts for decision
- Remote mode: ntfy notification with decision buttons
- Headless mode: auto-reject (current behavior preserved)
- On approve: full on_approve flow runs (PR, hooks, issues)
- On resume: limits adjusted, coordinator re-enters failing phase
- On reject: current ESCALATE terminal behavior
- escalate_review section in audit YAML
- Existing tests pass
- New tests for approve/reject/resume paths
- Default enabled in forge.yaml
