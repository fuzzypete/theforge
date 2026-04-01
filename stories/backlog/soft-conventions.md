---
name: "Soft conventions — prompt-injected project standards for plan/dev/review"
slug: soft-conventions
github_issue: 188
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

## Design

### forge.yaml schema

```yaml
conventions:
  soft:
    - "Single concern per module — each module should have one reason to change"
    - "Pure-data types (dataclasses, enums) in dedicated modules with stdlib-only imports"
    - "Separate prompt construction from story loading and output parsing"
```

Soft conventions are freeform strings. The project owner writes them in
whatever natural language makes sense for their codebase — TheForge does not
interpret them, it injects them.

### Prompt injection

Soft conventions are appended to the system context for three agent phases:

1. **Plan agent** — sees conventions when designing the implementation approach.
2. **Dev agent** — sees conventions as constraints during implementation.
3. **Review agent** — sees conventions as review criteria. Reviewers can cite
   convention violations as P2 findings.

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

`build_plan_prompt()`, `build_dev_prompt()`, `build_review_prompt()`, and
`build_fix_prompt()` gain a `conventions: list[str] | None` parameter. The
coordinator reads `conventions.soft` from config and passes it through. If
the list is empty or None, no conventions block is rendered.

### Review schema interaction

No schema changes. Convention violations are reported as normal P2 findings
where `description` references the convention.

## Acceptance Criteria

1. `forge.yaml` accepts a `conventions.soft` section as a list of strings
2. Soft conventions appear in plan agent prompts when configured
3. Soft conventions appear in dev agent prompts when configured
4. Soft conventions appear in review agent prompts when configured
5. Review agents can cite convention violations as P2 findings (no schema change)
6. Projects without soft conventions see no prompt changes
7. Conventions list is included in audit YAML for traceability
8. `make test` passes; `make lint` passes

## Non-goals

- Mechanical enforcement (that's hard conventions)
- Convention severity levels or categories (flat list)
- Per-phase convention filtering (all agents see all conventions)
- LLM-evaluated convention compliance scoring
