---
name: "Break up coordinator subsystem into its own package"
slug: coordinator-relocate-modules
pytest_target: tests/
---

# Break up coordinator subsystem into its own package

## Problem

`src/theforge/` has grown a large cluster of files that all belong to the
coordinator subsystem sitting alongside unrelated modules. This makes the
package hard to navigate and the coordinator hard to reason about in isolation.

## Goal

Reorganize the coordinator subsystem so it lives in its own package. All
existing tests must continue to pass without modification — nothing behavioral
changes, it's purely structural.

## Acceptance Criteria

- [ ] `make test` passes with the same test count.
- [ ] `make lint` passes.
- [ ] The coordinator subsystem files are no longer scattered at the top level
      alongside unrelated modules.
