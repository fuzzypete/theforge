---
name: "Dev review: trajectory tracking and surviving-family-aware fix prompt"
slug: dev-review-trajectory-tracking
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

## Family identity rules

A family is identified by a **seed anchor** — a single non-file anchor
selected from a valid match between two findings across cycles. When the
match provenance contains multiple shared non-file anchors, the seed is the
lexicographically first one. Once a family exists with seed anchor
`load_config`, any future finding that produces a valid match (per the full
`plan-finding-identity` matching rules) and whose shared anchors include
`load_config` joins that family. The seed anchor is frozen at family creation
time and never changes.

- If a finding matches multiple existing families, it joins the family with
  the longest history (most cycles). Ties broken by alphabetical order of
  seed anchor.
- If a finding has no anchor overlap with any prior-cycle finding, it remains
  unfamilied (classified as **new**). Families are only created upon a
  cross-cycle match — a single finding in isolation does not start a family.
- File-path-only overlap does not create or join a family — consistent with
  the `plan-finding-identity` matching constraint that file paths require a
  second anchor. A finding in `coordinator.py` and another in `coordinator.py`
  sharing no other anchor remain in separate families.

## State requirements

`CoordinatorState` must have a persisted per-family trajectory store,
separate from the existing rolling `cycle_history`. Each entry stores:

- The seed anchor identifying the family
- The cycle numbers where the family appeared
- A one-line description from each cycle's finding

This store must survive across all review cycles for the run. It is not
capped or rolled like `cycle_history`.

## Reuse of plan-finding-identity

This story reuses the full matching machinery from `plan-finding-identity` —
anchor extraction, pairwise matching, and provenance logging. The extraction
rules, anchor classes, and matching constraints (including file-path-only
insufficiency) are identical. Only the consumer differs: dev fix prompt
framing instead of plan regen disposition.

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
- Family creation and joining require a valid match per the full
  `plan-finding-identity` matching rules (not seed anchor overlap alone) —
  the seed anchor is the persisted key after a valid match, not a shortcut
  around matching
- File-path-only anchor overlap does not create or join a family
- A per-family trajectory store is persisted on `CoordinatorState`, recording
  seed anchor, cycle numbers, and one-line description per appearance
- When a finding matches multiple families, it joins the one with the longest
  history; ties broken alphabetically by seed anchor
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

## Notes

- **Cycle numbering gotcha:** Neither `state.review_cycle` nor
  `state.cycle_history_total` is a safe cycle key for trajectory snapshots.
  `review_cycle` is decremented on the exhausted-cycle continue path
  (`review_phase.py:700-701`) and reset to 0 on extend/reject
  (`review_phase.py:393, :419`). `cycle_history_total` is only incremented
  inside `_append_cycle_history()` (`completion.py:191-199`), which is NOT
  called on the exhausted-cycle continue path. The correct approach is to
  introduce a **dedicated monotonic trajectory counter** on
  `CoordinatorState` that is incremented exactly once when a review result
  is accepted for trajectory tracking.
- **Current vs historical families:** The fix prompt must distinguish
  families that are active in the CURRENT review cycle from families that
  survived in the past but are no longer appearing. Pass a current-cycle
  matched-family set (or the current trajectory cycle id) into
  `build_fix_prompt()` so framing switches only when a currently active
  family has survived 2+ cycles — not whenever any historical family
  reached that threshold.
- **Plan-approach guard contradiction:** When `plan_output` is present,
  `fix_prompts.py` emits "Do NOT redesign or adopt a different strategy."
  This directly contradicts the "reconsider the approach" framing for
  surviving families. When surviving families trigger the reframed prompt,
  suppress or replace the existing plan-approach guard so the agent
  receives one coherent set of instructions.
- **Anchor extraction scope:** Extract structural anchors from
  `finding.description` and the direct `finding.file` field only. Do NOT
  extract anchors from `finding.suggestion` — the reused
  `plan_finding_classifier.extract_anchors()` is description-only by
  design.
- **Family creation must seed both cycles:** When a family is first
  created from a valid cross-cycle match, record BOTH the prior-cycle
  appearance and the current-cycle appearance in the family's cycle list
  and descriptions. A family created from a cycle-1/cycle-2 match must
  show cycles `[1, 2]`, not just `[2]`.
- **Resume persistence via `save_sessions()`:**
  `src/theforge/sessions.py:17` rewrites `.forge/sessions.json` from
  scratch on every call. Multiple callers (`dev_phase.py`, `review_pool.py`,
  `plan_flow.py`, `engine.py`) invoke it and will drop trajectory keys
  unless those keys are preserved. Either teach `save_sessions()` to
  merge/retain extra keys, or ensure every caller passes the trajectory
  fields through so later writes do not erase them.
- **ReviewFinding has `file` and `line`** unlike `PlanReviewFinding`. Dev
  review findings will produce richer file-path anchors. The `file` field
  provides a direct anchor without needing to extract paths from description
  text. However, `ReviewFinding.file` can be placeholder strings like
  `"unknown"` or empty — only treat non-empty, path-like values as anchors.
- **Trajectory summary truncation:** If a family survives 5+ cycles, the
  per-cycle descriptions in the trajectory summary should be truncated to
  keep the fix prompt from growing unbounded.
