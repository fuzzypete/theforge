---
name: "Targeted fix prompt for review iterations"
slug: targeted-fix-prompt
pytest_target: tests/
---

# Targeted Fix Prompt

## Problem

When a review returns REQUEST_CHANGES, the coordinator loops back to dev with
the full original prompt — full spec, full preflight output, full implementation
instructions — plus the findings appended at the end. Dev treats it like a fresh
task: re-reads everything, re-orients, re-runs all checks.

But the dev session is already resumed. The agent has full context from iteration 1.
Sending the entire spec again is waste. Observed: iteration 2 burns as much time
and tokens as iteration 1 even for a targeted 2-line fix.

When a reviewer tells you to fix a P1, you don't re-read the entire spec. You fix
the P1.

## Requirements

1. When looping back to dev after REQUEST_CHANGES, the prompt contains only what
   the dev needs to fix: the findings, where they are, and what to do
2. When looping back after a gate failure, dirty worktree, or human reject, the
   full prompt is used — those paths need full context
3. The coordinator knows which path triggered the retry and routes accordingly
4. The fix prompt does not include spec content, preflight output, or
   implementation instructions

## Acceptance Criteria

- [ ] After REQUEST_CHANGES, dev receives a focused prompt with only the findings
      and fix instructions — no spec recap, no preflight output
- [ ] After gate failure or dirty worktree, dev receives the full prompt unchanged
- [ ] After human extend, dev receives a focused prompt (same as REQUEST_CHANGES)
- [ ] After human reject with feedback, dev receives the full prompt
- [ ] Routing is deterministic — no LLM decides which prompt to use
- [ ] All existing tests pass
