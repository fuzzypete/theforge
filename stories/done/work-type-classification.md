---
name: "Work-type classification — preflight signal for plan depth and review gating"
slug: work-type-classification
pytest_target: tests/
---

# Work-Type Classification

## Problem

Every failed refactor sprint died in plan review, not dev. The plan reviewer
found real gaps in import chains and dependency graphs, but these are problems
that can only be solved empirically by running the code — not by planning
harder. The plan-review loop ate itself: planner fixes gap A, reviewer finds
gap B, planner fixes B, reviewer finds C. Repeat until max_plan_regen_attempts
exhausted → ESCALATE.

Root cause: plan review applies feature-story rigor to mechanical work.

## Design

Preflight already classifies story **complexity** (small / medium / large).
It should also classify **work type**:

| Type | Description | Plan depth | Plan review |
|------|-------------|------------|-------------|
| `feature` | New capability, user-facing change | Full plan with step-by-step | Full review |
| `refactor` | Structural reorganization, no behavior change | File mapping only — what moves where | Advisory only (findings logged, never blocks) |
| `mechanical` | Rename, format, split, merge — zero judgment | Minimal or skip plan entirely | Skip |
| `bug` | Fix broken behavior, regression | Focused plan on root cause and fix | Full review |

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

### Preflight parsing

Add a `_parse_preflight_work_type()` helper (consistent with existing
`_parse_preflight_complexity()` pattern). Default to `feature` when the
field is missing or unrecognized, preserving current full-plan/full-review
behavior for existing stories.

## Acceptance Criteria

1. Preflight emits `WORK_TYPE: feature | refactor | mechanical | bug`
2. Coordinator reads work type and stores it in state
3. `refactor` work type: plan review is advisory (findings logged, never rejects)
4. `mechanical` work type: plan review is skipped entirely
5. Default (missing work type): `feature` behavior (full plan, full review)
6. Audit log records the classified work type
7. `make test` passes; `make lint` passes

## Non-goals

- Auto-detection of work type from code diff (preflight reads story text, not code)
- Overriding work type via forge.yaml or CLI flag (later if needed)
