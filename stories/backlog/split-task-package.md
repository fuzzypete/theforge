---
name: "Split task.py into a task/ package"
slug: split-task-package
pytest_target: tests/
---

# Split task.py into a task/ package

## Problem

`task.py` is 1,302 lines mixing story loading, frontmatter parsing, and prompt
builders for four distinct phases (plan, dev, review, fix). Each prompt family
evolves independently but edits to one risk breaking another because they share
a file.

## Solution

Convert `src/theforge/task.py` into a `src/theforge/task/` package, split by
prompt family.

### Target layout

```
src/theforge/task/
  __init__.py         — re-exports public API (TaskStory, load_story, etc.)
  story.py            — TaskStory, load_story, frontmatter parsing
  plan_prompts.py     — plan-phase prompt construction
  dev_prompts.py      — dev-phase prompt construction
  review_prompts.py   — review-phase prompt construction
  fix_prompts.py      — fix-phase prompt construction
  plan_parser.py      — plan output parsing
```

## Constraints

- Pure structural refactor — zero behavioral change.
- `from theforge.task import TaskStory, load_story` must continue to work.
- All existing imports from `theforge.task` must resolve unchanged.
- No new dependencies.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] All existing public imports from `theforge.task` still work.
- [ ] No single file in `src/theforge/task/` exceeds 400 lines.
- [ ] The old `task.py` file no longer exists.
