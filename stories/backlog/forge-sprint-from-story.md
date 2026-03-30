---
name: "forge sprint accepts story files directly; deprecate forge run"
slug: forge-sprint-from-story
pytest_target: tests/test_sprint_runner.py
---

# forge sprint accepts story files directly; deprecate forge run

## Problem

`forge` has two entry points for running stories: `forge run <story>` and
`forge sprint <manifest>`. This is confusing — users must know which to use,
and the two paths have diverged in capability (sprint has auto_merge,
parallel, budget tracking; run does not). The split also doubles the surface
area for bugs and behavioral inconsistencies.

## Expected behavior

`forge sprint` accepts a story file path directly in addition to a sprint
manifest:

```
forge sprint stories/backlog/my-story.md --verbose
```

When given a story file, the coordinator auto-wraps it in a single-story
sprint manifest at runtime (no file written to disk) and executes it through
the sprint path. All sprint features (budget tracking, audit, auto_merge,
hooks) apply.

`forge run` is deprecated with a visible warning that directs users to
`forge sprint`, and removed in a future release.

## Acceptance criteria

- `forge sprint <path>` where path ends in `.md` and the file exists is
  treated as a single-story sprint with a default budget
- The auto-generated manifest uses sensible defaults: `auto_merge: true`,
  `budget_usd` derived from `forge.yaml` default or a documented fallback
- `forge run` prints a deprecation warning on every invocation pointing to
  `forge sprint`
- All existing `forge sprint <manifest>` behavior is unchanged
- All existing tests pass
- New tests cover: story-file path detection, auto-manifest generation,
  deprecation warning on `forge run`
