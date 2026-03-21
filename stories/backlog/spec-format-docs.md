---
name: "Story format guidance in dev prompt and forge init"
slug: story-format-guidance
pytest_target: tests/
---

# Story format guidance in dev prompt and forge init

Dev agents get raw spec content but no guidance on how to read it. They
don't know that acceptance criteria are the normative checklist and
everything else is context. On complex stories this causes confusion —
the agent treats background discussion as requirements, or misses ACs
buried in prose.

Meanwhile `forge init` produces a bare `forge.yaml` but no story
template, so new projects have no reference for what a well-written
story looks like. And `docs/vision.md` still describes the old linear
pipeline with "specs" everywhere — it doesn't reflect the story-first
workflow that `forge ideate` + PLAN phase actually implements.

A story says WHAT and WHY. Acceptance criteria describe observable
behavior. The PLAN phase produces the HOW. The dev prompt should tell
agents this explicitly.

## Acceptance criteria

- `build_dev_prompt()` includes a preamble before spec content that
  identifies acceptance criteria as the definitive checklist
- Preamble tells the agent: context sections are not requirements,
  ACs are the checklist, flag ambiguity in handoff notes
- `forge init` creates `specs/TEMPLATE.md` alongside `forge.yaml`
  with annotated story structure (frontmatter, problem, ACs)
- `docs/vision.md` updated: pipeline diagram shows `--until`, upstream
  workflow section (brief → story → plan → dev), story vs spec
  terminology clarified
- New test verifies the preamble appears in dev prompt output
- Existing tests pass
