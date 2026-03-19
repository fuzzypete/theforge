---
name: "Spec format documentation and dev prompt guidance"
slug: spec-format-docs
file_scope:
  - src/theforge/task.py
  - src/theforge/cli.py
  - tests/test_task.py
pytest_target: tests/
---

# Spec Format Documentation

## Problem

There is no documentation for what a well-written spec looks like. The format
is defined implicitly by the `TaskSpec` dataclass, example specs in `specs/done/`,
and prompt builders in `task.py` — but none of this is surfaced to spec authors
or to the dev agent.

This causes two problems:

1. **Spec authors** (humans and `forge ideate`) have no reference for what
   sections to include, what makes good acceptance criteria, or what to avoid.
   Specs vary wildly in structure and quality.

2. **Dev agents** receive the raw spec content but no guidance on how to
   interpret it. They don't know which sections are normative (acceptance
   criteria) vs contextual (problem statement, background). This leads to
   confusion on complex specs — the agent may treat background discussion as
   implementation requirements, or miss acceptance criteria buried in prose.

## Solution

### 1. Create `SPEC_FORMAT.md` — canonical spec authoring reference

A reference document at the project root that documents:

- Required YAML frontmatter fields (`name`, `slug`) and optional fields
  (`file_scope`, `pytest_target`, `gate`, `depends_on`)
- Recommended markdown sections: Problem, Solution, Acceptance Criteria
- What makes good acceptance criteria (testable, unambiguous, checklist format)
- What to avoid (vague language, implementation details in ACs, mixing context
  with requirements)
- A complete annotated example spec

This file is for humans and for `forge ideate` to reference.

### 2. Add spec template to `forge init`

When `forge init` creates a new project, also create `specs/TEMPLATE.md` with
the canonical structure and inline comments explaining each section. This gives
new projects a starting point.

Modify `cli.py`'s init command to write the template file alongside `forge.yaml`.

### 3. Inject format guidance into the dev prompt

In `build_dev_prompt()` in `task.py`, add a short preamble before the spec
content that tells the agent how to read the spec:

```
## How to read this spec

The spec below describes what to implement. Key sections:

- **Problem / Background**: Context only — do not implement fixes for problems
  described here unless they appear in the acceptance criteria.
- **Solution**: The intended approach. Follow this unless you find a clearly
  better alternative, in which case note the deviation in your handoff.
- **Acceptance Criteria**: The definitive list of what must be done. Every
  criterion must be satisfied for the task to pass review. Treat these as your
  checklist.

If the spec is ambiguous or contradictory, implement the most reasonable
interpretation and flag the ambiguity in your handoff dev_notes.
```

This is injected once, before the spec content, in every dev prompt.

## Acceptance Criteria

- [ ] `SPEC_FORMAT.md` exists at project root with:
      - Frontmatter field reference (name, slug, file_scope, pytest_target,
        gate, depends_on) with types and defaults
      - Recommended sections (Problem, Solution, Acceptance Criteria)
      - Guidelines for writing good acceptance criteria
      - A complete annotated example spec
- [ ] `forge init` creates `specs/TEMPLATE.md` with annotated spec template
- [ ] `build_dev_prompt()` in `task.py` includes a "How to read this spec"
      preamble before the spec content
- [ ] The preamble clearly identifies acceptance criteria as the normative
      checklist
- [ ] New test in `test_task.py` verifies the preamble appears in dev prompt
      output
- [ ] Existing tests pass without modification
