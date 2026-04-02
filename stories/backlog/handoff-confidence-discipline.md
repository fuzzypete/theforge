---
name: "Handoff confidence discipline: fix claims require test or invariant backing"
slug: handoff-confidence-discipline
github_issue: 257
pytest_target: tests/
---

# Handoff Confidence Discipline: Fix Claims Require Test or Invariant Backing

## Problem

Dev handoff summaries sometimes assert that a bug is fixed with confident
language ("ensures X can never happen", "the gap is now closed") without
referencing a test that proves the claim or naming the invariant that was
checked. Reviewers then accept the framing and are less likely to probe the
edge case independently.

In a real run, a cycle-2 handoff stated the blood-pressure fix "ensures the
lagging component's gap is never silently skipped." Cycle 3 showed this was
false — the partial-anchor case was still unsafe. The overconfident claim may
have primed reviewers to look for different bugs rather than re-examine the
original one.

The handoff is dev self-reporting; TheForge cannot verify it. But it can detect
when a handoff contains unsubstantiated fix claims and flag them to reviewers
so they are treated as hypotheses rather than facts.

## Goal

When a dev handoff summary contains fix-claim language ("fixed", "resolved",
"ensures", "now prevents", "no longer") in the context of a prior P1 finding,
the coordinator checks whether the handoff also names a test or a specific
invariant check that backs the claim. If not, the reviewer prompt includes a
notice that the claim is unsubstantiated, so reviewers apply extra scrutiny to
the relevant code path.

## Acceptance Criteria

- The coordinator scans the handoff summary for fix-claim phrases adjacent to
  language from prior unresolved P1 findings (file name, function name, or key
  nouns from the finding description)
- When a fix claim is detected without a backing test name or invariant
  description in the same sentence or adjacent sentence, the reviewer prompt
  includes a flagged notice: "Dev claimed this finding fixed but cited no test
  or invariant — verify independently: [finding description]"
- When a fix claim includes a test name (matches `test_*`, `*Test`, `*Spec`, or
  a file path ending in test/spec) or an explicit invariant statement, no notice
  is added
- The notice is scoped to the specific finding the claim appears to address; it
  does not flag all fix claims globally
- The audit YAML records which claims were flagged and which were accepted as
  substantiated for each dev handoff
- All existing tests pass; new tests cover detected-unsubstantiated and
  detected-substantiated paths

## Out of Scope

- Verifying that a named test actually exists or passes (that is the gate's job)
- Modifying the dev prompt to require a specific handoff format
- Blocking the pipeline based on unsubstantiated claims — this is a reviewer
  signal, not a gate

## Notes

- Detection is heuristic and string-based — no LLM in the loop. False positives
  (flagging a well-substantiated claim) are acceptable; false negatives (missing
  an unsubstantiated claim) are also acceptable. The goal is to catch the
  obvious cases.
- The handoff summary is already parsed and available in coordinator state after
  each dev pass.
- Prior unresolved P1 finding descriptions are in `state.finding_registry` —
  key nouns can be extracted with the same `_normalize_tokens` function already
  in `finding_classifier.py`.
