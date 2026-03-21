---
name: "Remove file_scope from story format"
slug: drop-file-scope
pytest_target: tests/
---

# Remove file_scope from story format

Stories describe WHAT and WHY. The plan agent reads the codebase and
figures out WHERE. Hardcoding `file_scope` in frontmatter is the old
pre-grooming model — the human guesses which files need changing before
any agent has looked at the code.

This already caused problems: preflight returned BLOCKED on specs where
the scope was too narrow, and `forge ideate` had to invent file lists
without reading the codebase. The advisory softening (done) helped, but
the field still exists and agents still treat it as a constraint.

Drop it. The plan phase replaces it entirely.

## What changes

1. Remove `file_scope` from `TaskSpec` dataclass
2. Stop parsing `file_scope` from spec frontmatter (ignore if present)
3. Remove file_scope interpolation from `build_dev_prompt()` — no
   "Focus your changes on" section
4. Remove `SCOPE_BLOCKED` sentinel and `_parse_scope_blocked()` from
   task.py (dead code once file_scope is gone)
5. Remove scope-related logic from coordinator (scope_blocked handling)
6. Update `forge ideate` synthesis prompt to stop generating file_scope
7. Existing specs with `file_scope` in frontmatter still parse — the
   field is silently ignored, not an error

## Acceptance criteria

- `TaskSpec` has no `file_scope` field
- Specs with `file_scope` in frontmatter parse without error (ignored)
- Dev prompt contains no file scope restriction language
- `SCOPE_BLOCKED` sentinel removed from task.py
- `forge ideate` output does not include `file_scope` in frontmatter
- Coordinator does not check for scope_blocked in dev output
- All existing tests pass or are updated
