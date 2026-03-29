---
name: "Hard conventions — mechanically enforced code structure rules"
slug: hard-conventions
depends_on: [config-normalization]
pytest_target: tests/
---

# Hard Conventions

## Problem

Every refactoring story in the backlog exists because a structural rule was violated
silently — a module grew past 3,000 lines, circular imports crept in, test files
stopped mirroring source layout. These violations are only caught retroactively when
a human notices merge conflicts or context-window pressure, then writes a cleanup
story. By the time the refactoring runs, the project has already paid for the
wasted agent compute on oversized files.

These rules are deterministic. A line count is a line count. An import cycle is
detectable with static analysis. There is no reason to wait for an LLM to notice
them.

## Context

Evidence from completed and planned refactoring work:

- `coordinator.py` reached 3,200 lines before extraction stories were written
- `test_coordinator.py` reached 7,500 lines — five extraction stories to split it
- `cli.py` at 2,372 lines, `task.py` at 1,302, `config.py` at 1,184 — all queued for package splits
- Every extraction story documents one-way dependency graphs because circular imports were an implicit fear
- `structure-cleanup-facades` exists to remove re-export indirection that accumulated from ad-hoc splits

All of this work is preventable if the rules are stated upfront and checked mechanically.

## Design

### forge.yaml schema

```yaml
conventions:
  hard:
    max_module_lines: 500
    max_test_file_lines: 1000
    no_circular_imports: true
    test_mirrors_source: true
```

### New module: `src/theforge/conventions.py`

Provides a `check_hard_conventions()` function that:

1. **Line count check** — walks `src/` and `tests/` counting lines per `.py` file.
   Files exceeding the threshold are reported with current count and limit.
2. **Circular import check** — builds an import graph from `src/theforge/**/*.py`
   using AST parsing (`ast.parse` + walk `Import`/`ImportFrom` nodes). Reports
   any cycle as a list of modules in the cycle.
3. **Test mirror check** — for each `src/theforge/foo.py`, verifies
   `tests/test_foo.py` exists. For packages (`src/theforge/foo/`), verifies
   `tests/test_foo_*.py` or `tests/test_foo/` exists. Reports unmirrored modules.

Returns a list of `ConventionViolation` dataclasses:

```python
@dataclass
class ConventionViolation:
    rule: str          # "max_module_lines", "no_circular_imports", etc.
    file: str          # path relative to project root
    detail: str        # human-readable description
    blocking: bool     # True for hard conventions
```

### Integration with VALIDATE phase

The coordinator calls `check_hard_conventions()` after the gate command. If any
hard violations are returned, the gate fails with a structured report — same as
a test failure. The dev agent sees the violations in its next iteration and can
fix them.

Convention violations are included in the audit YAML under a `convention_violations`
key.

### Config loading

`conventions.hard` is parsed in `config.py` alongside existing config sections.
Missing keys use sensible defaults (the values above). If `conventions` is absent
entirely, no checks run — backward compatible.

## Acceptance Criteria

1. `forge.yaml` accepts a `conventions.hard` section with the four keys above
2. `check_hard_conventions()` detects files over the line limit and reports them
3. `check_hard_conventions()` detects circular imports via AST analysis
4. `check_hard_conventions()` detects missing test mirror files
5. Hard convention violations fail the VALIDATE gate with a structured message
6. Violations appear in audit YAML under `convention_violations`
7. Projects without a `conventions` section in forge.yaml see no behavior change
8. `make test` passes; `make lint` passes

## Non-goals

- Soft/prompt-injected conventions (separate story)
- Auto-fixing violations (agent sees the report and fixes on next iteration)
- Per-file or per-directory overrides (can add later if needed)
