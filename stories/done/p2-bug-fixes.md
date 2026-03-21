---
name: "P2 bug fixes — cost tracking, plan parsing, audit data shape"
slug: p2-bug-fixes
pytest_target: tests/
---

# P2 Bug Fixes

## Problem

Three confirmed bugs from review P2 findings across recent sprints:

1. **Cost tracking gap (#60)**: total_cost property on CoordinatorState does
   not include spec validation cost. Top-level cost totals underreport when
   spec validation runs.

2. **Plan YAML detection bug (#49)**: lstrip("-") is character-based, not
   substring-based. It strips individual characters from the left of the
   string, not the YAML document marker "---" as a token. A plan starting
   with a slug like "-home" would be corrupted.

3. **Audit data shape bug (#69)**: When plan validation runs and produces
   zero findings (clean pass), the plan_validation block in the audit log
   is set to None. This makes "clean pass" indistinguishable from
   "validation didn't run." Should be an empty findings list, not None.

Additionally, one code quality fix:

4. **Duration tracking (#52)**: Multiple uses of `or 0.0` conflate a
   legitimate zero-second duration with "data not available." Should use
   explicit None checks.

## Acceptance Criteria

- [ ] total_cost includes spec_validation cost when present
- [ ] YAML document marker detection uses string comparison, not lstrip
- [ ] Plan validation audit block present with empty findings on clean pass,
      absent only when validation did not run
- [ ] Duration fields use `is not None` checks instead of `or 0.0`
- [ ] All existing tests pass
- [ ] New tests for each fix
