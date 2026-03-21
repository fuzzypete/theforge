---
name: "Plan validation — mechanical structure check before DEV"
slug: plan-validation
pytest_target: tests/
---

# Plan Validation

## Problem

Plans can be structurally incomplete — missing ACs, referencing nonexistent
files, having steps with no details — and still pass plan review because the
LLM reviewer focuses on approach quality, not structural coverage. Dev budget
is then spent on a plan that was mechanically deficient.

The codebase already has `PlanData` (TypedDict in task.py) and
`parse_plan_output()` which parses YAML plans with steps, criteria_mapping,
and file targets. When the plan agent produces structured YAML, the
coordinator can validate plan structure deterministically before DEV runs.

## Solution

After PLAN produces structured YAML output and before DEV starts, run a
mechanical validation pass (pure Python, no LLM):

1. **AC coverage**: every acceptance criterion from the spec frontmatter
   appears in criteria_mapping. Missing ACs → WARN.
2. **Step completeness**: every step has description, at least one file,
   and an action type. Empty steps → WARN.
3. **File validity**: files referenced in steps exist in the workspace
   (or action=create). Referencing nonexistent files for modify → WARN.
4. **Dependency ordering**: depends_on references valid step IDs, no
   circular dependencies.

Validation is advisory — WARN is logged, run continues to DEV. Findings
are recorded in the audit log under `plan_validation`.

Skipped when plan is freeform markdown (fallback mode) since there's
nothing structured to validate.

## Acceptance Criteria

- [ ] Mechanical plan validation runs after PLAN, before DEV
- [ ] AC coverage check: warns on unmapped acceptance criteria
- [ ] Step completeness check: warns on steps missing description/files/action
- [ ] File validity check: warns on modify/delete for nonexistent files
- [ ] Dependency check: warns on invalid step refs or circular deps
- [ ] Validation is advisory — never blocks DEV
- [ ] Skipped for freeform markdown plans (no structured data)
- [ ] Findings logged to console and recorded in audit log
- [ ] All existing tests pass
- [ ] New tests for each validation check
