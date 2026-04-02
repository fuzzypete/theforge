---
name: Adaptive assignment audit trail
slug: adaptive-assignment-audit-trail
pytest_target: tests/
depends_on: [adaptive-model-assignment, audit-improvements]
---

# Adaptive Assignment Audit Trail

## Problem

Adaptive assignment decisions are difficult to trust after the fact because the
post-run audit log does not expose the routing decision in one coherent place.
An operator currently has to reconstruct what happened from code, verbose log
lines, and config defaults.

The audit should answer, in one pass:

- which planner/dev/reviewers were chosen
- which roles were preserved because of explicit overrides
- the rationale for each role decision
- whether escalation-memory promotion fired

Without that, every adaptive-assignment investigation becomes code-reading.

## Acceptance criteria

- `generate_audit_log()` includes an `assignment` block when adaptive
  assignment is active
- The assignment block records chosen planner, dev, plan reviewers, and code
  reviewers
- The assignment block records which roles were preserved as explicit overrides
- The assignment block records rationale strings per role
- The assignment block records whether promotion fired and, if so, from what
  base tier/model to what promoted tier/model
- The assignment block is omitted or `null` when adaptive assignment is not active
- `forge audit` renders a concise adaptive-assignment summary when present
- New tests cover audit serialization and display for adaptive decisions
