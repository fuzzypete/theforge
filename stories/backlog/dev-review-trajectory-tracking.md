---
name: "Dev review: trajectory tracking and surviving-family-aware fix prompt"
slug: dev-review-trajectory-tracking
depends_on: [plan-finding-identity]
pytest_target: tests/test_review_phase.py
---

# Dev review: trajectory tracking and surviving-family-aware fix prompt

## Problem

When a dev review cycle rejects code multiple times, the dev agent receives
"fix these findings" each iteration with no awareness that the same issue
family has survived across cycles. The agent optimizes for local patching —
addressing the latest finding text — rather than recognizing a repeated
failure mode.

The plan regen loop had the same problem (`strict_auth` surviving four
attempts). The `plan-regen-trajectory-tracking` story fixed it for plans. The
dev/review loop needs the same treatment.

Real example: `plan-finding-identity` went through three cycles around the
file_path/dotted_path classification boundary. Cycle 1: unknown-extension
filenames false-matched as dotted paths. Cycle 2: the fix was too broad,
blocking valid dotted paths. Cycle 3: narrowed guard, but the same boundary
defect survived. Three different findings, one unresolved design tension. The
coordinator treated them as separate review comments, so the dev agent kept
doing local edits.

## Expected behavior

After each dev review cycle, the coordinator uses the structural anchor model
(from `plan-finding-identity`) to match findings across cycles. When it
detects that the same issue family has survived across 2+ cycles, it switches
the fix prompt from "patch these findings" to "this design tension has
persisted across N cycles — reconsider the approach."

Specifically:

- After each review merge, extract anchors from current findings and match
  against prior-cycle findings using the anchor model
- Classify each finding family as **new** (no anchor overlap with prior
  cycles) or **surviving** (shares anchors with a finding from a prior cycle)
- When surviving findings are detected after 2+ cycles, the fix prompt
  includes a trajectory summary and switches framing

## State requirements

`CoordinatorState` must have a persisted per-family trajectory store,
separate from the existing rolling `cycle_history`. Each entry stores:

- The anchor set identifying the family
- The cycle numbers where the family appeared
- A one-line description from each cycle's finding

This store must survive across all review cycles for the run. It is not
capped or rolled like `cycle_history`.

## Relationship to existing finding registry

Anchor-based family tracking **augments** the existing hash-based
`finding_registry` and `classified_p1s` pipeline — it does not replace
either. The hash-based registry continues to provide per-finding dispositions
(new, persistent, resolved). Anchor families are a new overlay used solely
for trajectory detection and prompt framing. The two systems may classify
findings differently and that is expected.

## Acceptance criteria

- After each review merge, the coordinator extracts structural anchors from
  findings and matches them against prior-cycle findings using the anchor
  model from `plan-finding-identity`
- Each finding family is classified as new or surviving based on anchor
  overlap — no semantic or prose-based judgment
- A per-family trajectory store is persisted on `CoordinatorState`, recording
  anchor set, cycle numbers, and one-line description per appearance
- The fix prompt includes a trajectory summary when surviving findings are
  detected after 2+ cycles
- The trajectory summary includes: the anchor(s) identifying the family, the
  one-line description from each cycle where it appeared, and the number of
  cycles it has survived
- When surviving findings are present, the fix prompt switches from "fix each
  P1 finding" to "reconsider the approach for this boundary" framing
- The existing `cycle_history`, `classified_p1s`, and `finding_registry` in
  the fix prompt are preserved — trajectory context is additive
- All existing tests pass
- New tests cover: anchor extraction from review findings, family
  classification across synthetic cycle sequences, prompt content for
  surviving vs new-only scenarios
