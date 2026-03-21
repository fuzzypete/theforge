---
name: "Dev observability and plan anchoring across fix iterations"
slug: dev-observability-and-anchoring
pytest_target: tests/
---

# Dev Observability and Plan Anchoring

## Problem

When a dev agent fails to converge — introducing regressions or ignoring
P1s across review cycles — we can't diagnose why because:

1. Dev traces (prompt and output) are only captured for the first iteration.
   Fix cycles have no record of what the agent received or produced.

2. The dev handoff (what the agent claims it addressed) is a transient file
   in the worktree. It's never recorded in the audit trail. Once the worktree
   is cleaned up, the evidence is gone.

3. Fix iteration prompts don't anchor the dev agent to the approved plan.
   When reviewers flag P1s, the agent sometimes redesigns its approach
   instead of fixing within the plan's framework. This causes flip-flopping
   across cycles — each fix introduces new problems because the agent is
   trying a different strategy each time.

## Acceptance Criteria

- [ ] Dev prompt and output traces written for every iteration, not just
      the first. Filenames include iteration number.
- [ ] Dev handoff (acceptance criteria status, dev notes) captured in the
      audit trail per iteration, not just in the worktree file.
- [ ] Fix iteration prompts re-inject the approved plan and explicitly
      instruct the agent to fix within the plan's approach, not redesign.
- [ ] Fix iteration prompts list each P1 finding with its disposition
      (unresolved, regression, new) so the agent knows what to prioritize.
- [ ] Audit trail supports post-mortem comparison: P1s sent to dev vs
      what dev claimed to address vs what reviewer found still broken.
- [ ] All existing tests pass.
- [ ] New tests for trace capture on iteration 2+ and handoff in audit.
