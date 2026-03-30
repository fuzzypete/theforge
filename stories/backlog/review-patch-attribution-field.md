---
name: "Add patch_attribution field to review schema for causal blocking"
slug: review-patch-attribution-field
pytest_target: tests/
---

# Review Schema: Explicit Patch Attribution

## Problem

The coordinator currently infers whether a P1 finding is causally attributable to
the patch using a same-file heuristic: findings in changed files → `regression`
(blocking); findings in untouched files → `net_new` or `corroborated_new`
(non-blocking on cycle 2+).

This heuristic breaks for cross-file regressions. When a patch changes a callee's
contract and breaks an existing caller in an untouched file, the finding is
causally attributable to the patch but the coordinator classifies it as `net_new`
and does not block on cycle 2+. The reviewer prompt correctly instructs reviewers
that "the defect does NOT need to be in a file modified by these commits — if the
patch caused it, it is P1 regardless of where the symptom appears," but the
coordinator overrides that judgment with a structural heuristic.

As a workaround, cycle 1 trusts reviewer P1 judgments directly (any P1 blocks).
But cycle 2+ still uses the heuristic — meaning a cross-file regression that
survives cycle 1 could be downgraded to non-blocking on subsequent cycles if the
reviewer continues reporting it as P1.

## Solution

Add a `patch_attribution` field to the review YAML schema so reviewers encode
causality explicitly. The coordinator uses this field as the primary blocking
signal, falling back to the file-overlap heuristic only when the field is absent
(for backward compatibility with older reviewer outputs or parse failures).

### Schema addition

```yaml
findings:
  - severity: P1
    file: "src/bar.py"
    line: 42
    description: "Caller breaks because callee contract changed"
    suggestion: "Update callee to preserve existing return type"
    patch_attribution: activated   # new field
```

Valid values:
- `introduced` — patch added the defective code
- `worsened` — patch made a pre-existing issue worse
- `activated` — patch created a new call path that triggers a latent bug
- `unresolved` — patch claimed to fix this but did not
- `pre_existing` — real issue, not caused by this patch

### Blocking logic

The coordinator uses `patch_attribution` as the primary signal:
- `introduced | worsened | activated | unresolved` → blocking (regardless of file)
- `pre_existing` → non-blocking (record in audit trail, downgrade to P2 in display)
- absent / unparseable → fall back to current file-overlap heuristic

This makes the gate mechanical and removes the need for the cycle-1 special case.

### Prompt update

The review prompt's severity definitions section gains a "How to fill
`patch_attribution`" subsection explaining when to use each value.

## Acceptance criteria

- `patch_attribution` is an optional field in `ReviewFinding` with values
  `introduced | worsened | activated | unresolved | pre_existing`
- Schema validation accepts and rejects invalid values; field is optional
- `has_blocking_p1` uses `patch_attribution` when present; falls back to
  file-overlap heuristic when absent
- A `pre_existing`-attributed P1 is non-blocking and appears in the audit
  trail with its original severity noted
- Review prompt explains how to fill `patch_attribution` for each value
- The cycle-1 special case in `review_phase.py` is removed once this lands
  (the attribution field makes it redundant)
- Tests cover: schema parse with/without field, blocking logic with each
  attribution value, fallback to heuristic when field absent

## Notes

- `schemas.py` owns the review YAML validation; add `patch_attribution` there
- `review.py` owns `ReviewFinding`; add the optional field there
- `finding_classifier.py` owns blocking logic; update `has_blocking_p1` there
- Review prompts live in `src/theforge/task/review_prompts.py`
- The cycle-1 trust workaround is in `src/theforge/coordinator/review_phase.py`
  around the `if state.review_cycle == 1` branch
