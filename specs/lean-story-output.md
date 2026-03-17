---
name: "Lean story output from forge ideate"
slug: lean-story-output
file_scope:
  - src/theforge/ideate.py
  - tests/test_ideate.py
pytest_target: tests/
---

# Lean Story Output

## Problem

`forge ideate` produces specs that are full of implementation detail: exact
dataclass definitions, function signatures, code snippets, file-level
implementation steps. These read like design docs, not stories.

This is backwards. The PLAN phase exists specifically to derive the HOW from
the codebase + the story. When ideate pre-solves the implementation, it:

1. **Robs PLAN of its job** — the plan agent re-derives what ideate already
   guessed, or worse, blindly follows ideate's guesses without checking them
   against the actual codebase
2. **Bakes in stale assumptions** — ideate doesn't read the codebase, so its
   function signatures and dataclass shapes may not match reality
3. **Makes stories brittle** — implementation-heavy stories break when the
   codebase changes, even if the requirement is still valid
4. **Inflates prompt size** — dev and plan agents receive bloated specs full
   of detail they'll re-derive anyway

A story should say WHAT and WHY. Acceptance criteria should describe
observable behavior, not internal structure. The PLAN phase produces the HOW.

## Requirements

1. The synthesis prompt must instruct the model to produce behavioral
   acceptance criteria — things a human or test can observe from outside —
   not implementation prescriptions
2. The synthesis prompt must explicitly prohibit: function signatures,
   dataclass/class definitions, code snippets, file-internal implementation
   steps, and specific variable/parameter names
3. `file_scope` in frontmatter is still required (it tells the coordinator
   which files are in play) but the story body should not describe what to
   do inside those files
4. The single-model prompt (`_build_single_model_prompt`) must follow the
   same constraints
5. The phase 1 prompt is unchanged — ideation should still think freely
   about implementation approaches during deliberation. The constraint
   applies at synthesis output only.
6. Stories may include a "## Context" or "## Background" section describing
   the current state of affairs to orient the plan agent, but this section
   describes what IS, not what to BUILD

## Acceptance Criteria

- [ ] `forge ideate` output contains no function signatures or code blocks
- [ ] Acceptance criteria in generated stories describe observable behavior
      (e.g., "sprint audit includes execution plan") not internal structure
      (e.g., "ExecutionPlan dataclass has batches field")
- [ ] `file_scope` is still populated in frontmatter
- [ ] Phase 1 ideation prompts are unchanged (models can still discuss
      implementation freely during deliberation)
- [ ] Single-model path produces the same lean format
- [ ] Generated stories are under 150 lines (current specs average 200-400)
- [ ] Existing ideate tests pass or are updated to match new output format
- [ ] A round-trip test: `forge ideate` → `forge run --dry-run` on the output
      produces a valid dev prompt (the story is parseable and usable)

## Out of Scope

- Changing existing specs in `specs/` — they work, leave them
- Changing the PLAN phase prompts — PLAN already produces the HOW
- Renaming `specs/` directory to `stories/` — separate decision
- Changing the frontmatter schema — `file_scope`, `slug`, `name`,
  `pytest_target` all stay
