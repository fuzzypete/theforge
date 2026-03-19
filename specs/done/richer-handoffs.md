---
name: "Richer handoffs — dev→reviewer and reviewer→dev"
slug: richer-handoffs
pytest_target: tests/
---

# Richer Handoffs

## Problem

Handoffs between dev and reviewer are thin in both directions. Each review cycle
starts cold: the reviewer doesn't know why the dev made specific decisions, and
the dev gets back a flat findings list with no orientation on what actually matters.

The result is mechanical spec-checking instead of meaningful review. Reviewers
flag correct decisions as violations because they have no context. Dev makes
shallow fixes because it doesn't know which findings are the root cause vs
symptoms.

In a real PR, the developer writes a description and the reviewer reads it before
looking at the diff. That context changes what gets flagged. And a good reviewer
tells you what to fix, not just what's wrong.

## Requirements

1. Dev writes a notes section in the handoff before finishing — what was
   implemented, any intentional spec deviations with justification, key
   trade-offs, what was deferred and why
2. The reviewer receives the dev notes before examining the diff
3. If a deviation is justified in dev notes, the reviewer does not flag it as
   a spec violation
4. The feedback from reviewer back to dev is action-oriented: summary of what
   to fix, spec compliance issues, test gaps, and findings with suggestions —
   not a restatement of the verdict
5. The dev receives the full reviewer summary and spec/test analysis, not just
   the raw findings list

## Acceptance Criteria

- [ ] Dev handoff includes a notes section describing implementation decisions
- [ ] Reviewer prompt includes dev notes before the diff
- [ ] Reviewer feedback to dev includes: overall summary, spec mismatches (if
      any), test gaps (if any), and findings with fix suggestions
- [ ] Sections with no content are omitted from reviewer feedback
- [ ] Dev notes absent from handoff → reviewer prompt unchanged (no empty section)
- [ ] All existing tests pass
