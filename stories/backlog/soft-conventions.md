---
name: "Soft conventions — prompt-injected project standards for plan/dev/review"
slug: soft-conventions
depends_on: [hard-conventions]
pytest_target: tests/
---

# Soft Conventions

## Problem

Hard conventions catch structural violations mechanically, but many project
standards require judgment — "single concern per module", "separate IO from
logic", "signal decomposition for oversized stories". These rules live in
CLAUDE.md today, which only Claude-family agents see. Codex, Gemini, and
DeepSeek reviewers have no access to these norms. Even Claude agents don't
reliably follow CLAUDE.md guidance during dev because it competes with the
task prompt for attention.

The result: dev agents produce code that passes tests but violates project
architecture. Review agents can't cite a convention they were never given.
Refactoring stories get written after the fact.

## Context

Patterns extracted from refactoring stories that require LLM judgment to enforce:

- **Single concern per module** — root cause of every coordinator extraction
- **Pure-data types at dependency graph leaves** — `coord_state.py` pattern
- **Prompt construction separate from loading/parsing** — `task.py` split rationale
- **Fire-and-forget vs. interactive in separate modules** — `coord_notify.py` rationale
- **Plan steps must map to acceptance criteria** — `structured-plan-output` story
- **Signal decomposition when scope exceeds threshold** — `plan-decompose-signal` story

These are too nuanced for line-count checks but concrete enough that any competent
agent can follow them if stated clearly in the prompt.

**Notes section convention** is already live: stories can include a `## Notes`
section with informal hints (file paths, gotchas). Plan and dev prompts tell
agents to treat Notes as potentially stale and verify against the codebase.
Soft conventions are the broader version of this — project-wide norms injected
into every prompt, not per-story hints.

## Design

### forge.yaml schema

```yaml
conventions:
  soft:
    - "Single concern per module — each module should have one reason to change"
    - "Pure-data types (dataclasses, enums) in dedicated modules with stdlib-only imports"
    - "Separate prompt construction from story loading and output parsing"
    - "Fire-and-forget operations (notifications) separate from interactive flows"
    - "Plan steps must map back to story acceptance criteria"
    - "Signal decomposition if story touches 6+ files or crosses 3+ module boundaries"
```

Soft conventions are freeform strings. The project owner writes them in
whatever natural language makes sense for their codebase — TheForge does not
interpret them, it injects them.

### Prompt injection

Soft conventions are appended to the system context for three agent phases:

1. **Plan agent** — sees conventions when designing the implementation approach.
   This is the highest-leverage injection point: a plan that respects module
   boundaries produces code that doesn't need refactoring.

2. **Dev agent** — sees conventions as constraints during implementation.
   Less effective than plan-phase injection (dev is already following a plan)
   but catches cases where the plan was vague.

3. **Review agent** — sees conventions as review criteria. Reviewers can cite
   convention violations as P2 findings with the convention text as the rule
   reference. This closes the loop: violations that slip past plan and dev
   get caught in review.

### Injection format

Conventions are rendered as a labeled block in the prompt:

```
## Project Conventions

The following conventions apply to this project. Respect them in your work
and flag violations you observe.

1. Single concern per module — each module should have one reason to change
2. Pure-data types (dataclasses, enums) in dedicated modules with stdlib-only imports
...
```

### Prompt builder changes

`build_plan_prompt()`, `build_dev_prompt()`, and `build_review_prompt()` in
`task.py` gain a `conventions: list[str]` parameter. The coordinator reads
`conventions.soft` from config and passes it through. If the list is empty,
no conventions block is rendered.

### Review schema interaction

No schema changes. Convention violations are reported as normal P2 findings
where `description` references the convention. This keeps the review schema
stable while giving reviewers a vocabulary for architectural feedback.

## Acceptance Criteria

1. `forge.yaml` accepts a `conventions.soft` section as a list of strings
2. Soft conventions appear in plan agent prompts when configured
3. Soft conventions appear in dev agent prompts when configured
4. Soft conventions appear in review agent prompts when configured
5. Review agents can cite convention violations as P2 findings (no schema change)
6. Projects without soft conventions see no prompt changes
7. Conventions list is included in audit YAML for traceability
8. `make test` passes; `make lint` passes

## Work-Type Classification and Plan Calibration

Preflight already classifies story **complexity** (small / medium / large).
It should also classify **work type**:

| Type | Description | Plan depth | Plan review |
|------|-------------|------------|-------------|
| `feature` | New capability, user-facing change | Full plan with step-by-step | Full review |
| `refactor` | Structural reorganization, no behavior change | File mapping only — what moves where | Advisory only (findings logged, never blocks) |
| `mechanical` | Rename, format, split, merge — zero judgment | Minimal or skip plan entirely | Skip |
| `bug` | Fix broken behavior, regression | Focused plan on root cause and fix | Full review |

### Why this matters

Every failed refactor sprint died in plan review (not dev). The plan reviewer
found real gaps in import chains and dependency graphs, but these are problems
that can only be solved empirically by running the code — not by planning
harder. The plan-review loop ate itself: planner fixes gap A, reviewer finds
gap B, planner fixes B, reviewer finds C. Repeat until `max_plan_regen_attempts`
exhausted → ESCALATE.

The root cause: plan review was applying feature-story rigor to mechanical work.

### Implementation

Preflight output gains a `WORK_TYPE: <type>` line alongside the existing
`COMPLEXITY: <size>` line. The coordinator reads it and adjusts:

- **Plan prompt**: for `refactor` / `mechanical`, inject "produce a high-level
  file mapping only — do not specify implementation steps, import fixes, or
  line-level changes. The dev agent will discover those empirically."
- **Plan review**: for `refactor`, run advisory-only (log findings, never
  reject). For `mechanical`, skip plan review entirely.
- **Dev prompt**: unchanged — dev always gets the full story and plan.
- **Code review**: unchanged — reviewers always evaluate the end state.

This is a small delta on existing machinery. Preflight already emits
structured output that the coordinator parses. Work type is one more field.

### Acceptance Criteria (additional)

- Preflight emits `WORK_TYPE: feature | refactor | mechanical | bug`
- Coordinator reads work type and adjusts plan depth accordingly
- `refactor` work type: plan review is advisory (findings logged, never rejects)
- `mechanical` work type: plan review is skipped entirely
- Default (missing work type): `feature` behavior (full plan, full review)
- Audit log records the classified work type

## Non-goals

- Mechanical enforcement of soft conventions (that's what hard conventions are for)
- Convention severity levels or categories (keep it simple — a flat list)
- Per-phase convention filtering (all agents see all conventions; simplicity wins)
- LLM-evaluated convention compliance scoring (reviewer judgment is sufficient)
- Auto-detection of work type from code diff (preflight reads the story text, not code)
