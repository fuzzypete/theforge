---
name: "Dev review: trajectory tracking and oscillation-aware fix prompt"
slug: dev-review-trajectory-tracking
depends_on: [plan-finding-identity]
pytest_target: tests/test_review_phase.py
---

# Dev review: trajectory tracking and oscillation-aware fix prompt

## Problem

When a dev review cycle rejects code multiple times, the dev agent receives
"fix these findings" each iteration with no awareness that the same issue
family has survived or oscillated across cycles. The agent optimizes for local
patching — addressing the latest finding text — rather than recognizing a
repeated failure mode.

The plan regen loop had the same problem (`strict_auth` surviving four
attempts). The `plan-regen-trajectory-tracking` story fixed it for plans. The
dev/review loop needs the same treatment.

Real example: `plan-finding-identity` oscillated for three cycles around the
file_path/dotted_path classification boundary. Cycle 1: unknown-extension
filenames false-matched as dotted paths. Cycle 2: the fix was too broad,
blocking valid dotted paths. Cycle 3: narrowed guard, but the same boundary
defect survived. Three different findings, one unresolved design tension. The
coordinator treated them as separate review comments, so the dev agent kept
doing local edits.

## Expected behavior

After each dev review cycle, the coordinator uses the structural anchor model
(from `plan-finding-identity`) to match findings across cycles. When it
detects that the same issue family has oscillated or survived multiple cycles,
it switches the fix prompt from "patch these findings" to "this boundary has
been the problem across N cycles — reconsider the approach."

Specifically:

- After each review merge, extract anchors from current findings and match
  against the finding registry from prior cycles
- Classify each finding family as **new**, **surviving** (same anchors,
  same direction), or **oscillating** (same anchors, fix introduced the
  inverse problem)
- When surviving or oscillating findings are detected after 2+ cycles,
  the fix prompt includes a trajectory summary and switches framing from
  "fix each P1 finding" to "the following design tension has not been
  resolved by local patches — rethink the approach for this specific
  boundary"

## Acceptance criteria

- After each review merge, the coordinator extracts structural anchors from
  findings and matches them against prior-cycle findings using the anchor
  model from `plan-finding-identity`
- Each finding family is classified as new, surviving, or oscillating
- The fix prompt includes a trajectory summary when surviving or oscillating
  findings are detected after 2+ cycles
- The trajectory summary includes: the anchor(s) identifying the issue family,
  a one-line description from each cycle where it appeared, and whether it
  is surviving or oscillating
- When oscillation is detected, the fix prompt switches from "fix each P1
  finding" to "reconsider the approach for this boundary" framing
- The existing cycle_history and classified_p1s in the fix prompt are
  preserved — trajectory context is additive, not a replacement
- All existing tests pass
- New tests cover: anchor extraction from review findings, family
  classification across synthetic cycle sequences, prompt content for
  surviving vs oscillating vs new-only scenarios
