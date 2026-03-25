---
name: "Add import boundary tests to prevent structural regression"
slug: "refactor-import-boundary-enforcement"
pytest_target: tests/
---

# Add import boundary tests to prevent structural regression

## Problem

Previous refactoring attempts regressed because nothing enforced the intended architecture. Modules drifted back into circular imports, re-export chains grew, and god files reformed. Without mechanical enforcement, any package structure will erode under normal development pressure.

## Requirements

- Add tests that mechanically assert the intended dependency graph: `sprint → coordinator → runners`, no reverse edges.
- Tests should detect circular imports between packages.
- Tests should detect backward-compat re-export chains (symbols imported only to re-export).
- Tests should flag any new top-level module added to `src/theforge/` without an explicit decision (prevents silent sprawl).
- Tests must be fast and run as part of `make test`.

## Acceptance Criteria

- [ ] A test file exists that asserts the package dependency graph
- [ ] Circular imports between coordinator/ and runners/ are caught by tests
- [ ] Adding a new top-level module without updating an allow-list fails the test
- [ ] `make test`, `make lint`, and `make gate` pass
- [ ] Tests run in under 2 seconds
