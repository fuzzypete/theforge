---
name: "net_new_pass must not override P1s that violate acceptance criteria"
slug: net-new-ac-violation-blocks
github_issue: 274
pytest_target: tests/
---

# net_new_pass Must Not Override AC-Violating P1s

## Problem

The `net_new_pass` disposition gate was designed to prevent infinite retry cycles
caused by reviewers introducing genuinely new, low-confidence P1s on late cycles —
findings that touch files the dev agent never modified and that no other reviewer
corroborated. This is a legitimate use case.

The gate has an unintended exemption: it applies to *any* net-new P1, including
ones that a reviewer explicitly ties to a violated acceptance criterion. When a
finding describes a behavior that the story's own acceptance criteria say must not
happen, the finding is not speculative — it is a direct assertion that the story
was not completed correctly. Treating it as non-blocking and shipping the PR
contradicts the review process's purpose.

A concrete example: the `on-approve-merge-pr` story required that
`on_approve: merge` and `on_approve: pr` behavior remain unchanged. A cycle-2
reviewer found that the new `merge_strategy` validation ran for all modes and
raised `ValueError` on previously valid configs — a direct AC violation. The
`net_new_pass` gate classified it as non-blocking because it was introduced in the
fix cycle, not the original implementation. The PR was created with this regression
in it.

## Goal

A P1 finding that cites a violated acceptance criterion always blocks, regardless
of its disposition classification. The `net_new_pass` path is narrowed so that it
cannot suppress findings where the reviewer indicates an AC was not met.

## Acceptance Criteria

- A P1 finding that causes `story_compliance.matches_spec: false` in a review
  output is always treated as blocking, even when its disposition is `net_new`
- A run that would otherwise take the `net_new_pass` path is blocked and returns
  `REQUEST_CHANGES` when any net-new P1 is associated with `matches_spec: false`
- The audit trail records that the finding was AC-blocking (not merely net_new)
- `net_new_pass` continues to work as designed for P1s where
  `story_compliance.matches_spec: true` — no regression to the convergence gate's
  primary purpose
- Existing tests pass
- New tests cover: net-new P1 with matches_spec false → blocks; net-new P1 with
  matches_spec true → does not block

## Out of Scope

- Semantic deduplication of findings across cycles (the distinct problem of
  carried-forward findings with reworded descriptions — a separate story)
- Changing how `matches_spec` is determined by reviewers
- Any modification to reviewer prompts

## Notes

- The blocking check in `review_phase.py` uses `_fc.net_new_p1s(_classified)` to
  identify non-blocking P1s. The fix is to filter that list: any net-new P1 whose
  reviewer's `story_compliance.matches_spec` is `false` is excluded from the
  non-blocking set and treated as blocking.
- The association between a specific finding and `matches_spec` is at the
  reviewer level, not the finding level. A conservative rule: if *any* reviewer
  in the pool returned `matches_spec: false`, treat all net-new P1s from that
  reviewer as AC-blocking. This avoids requiring per-finding AC attribution.
- `cycle_results` in `review_phase.py` contains `(reviewer_name, ReviewResult)`;
  `ReviewResult` already carries `story_matches` (bool). The fix can read this
  directly without schema changes.
