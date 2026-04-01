---
name: "Sprint DAG rejects depends_on slugs that are already merged to main"
slug: sprint-dag-satisfied-deps
pytest_target: tests/test_sprint_runner.py
---

# Sprint DAG rejects depends_on slugs that are already merged to main

## Observed behavior

Running a single-story sprint where the story's `depends_on` references a
slug not in the manifest raises a hard error, even when that dependency has
already been merged to main:

```
ValueError: Story 'X' depends on unknown slug(s): Y.
All depends_on slugs must reference stories in the sprint manifest.
```

## Expected behavior

The DAG treats a `depends_on` slug as satisfied if the corresponding branch
has already been merged to main. The dependency only needs to be in the
manifest if it hasn't shipped yet.

## Notes

- **Do NOT detect merges via git rev-list or branch ref comparison.** After a
  fast-forward merge, `base..branch` has 0 commits (identical refs), so
  `rev-list --count` returns 0 for genuinely merged branches. The feature
  branch ref may not even exist after cleanup. Four prior dev cycles failed
  trying this approach.
- **Simplest correct approach:** any `depends_on` slug that is NOT in the
  current sprint manifest should be treated as satisfied. If it's listed as a
  dependency but not in this sprint, it either already shipped or the user
  accepted the risk. The DAG only needs to sequence slugs that are both
  dependencies AND in the manifest.
- **Beware slug reuse in history.jsonl:** don't fall back to checking
  `history.jsonl` for a prior APPROVE record keyed by slug alone — slugs can
  be reused across sprints. If you use history at all, scope the lookup to the
  current sprint or branch.
