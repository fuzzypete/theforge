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
