---
name: "Restructure theforge into coordinator and runners packages"
slug: "coordinator-runners-package-split"
pytest_target: tests/
---

# Restructure theforge into coordinator and runners packages

## Problem

The `theforge` source tree has grown past the point where a flat module layout communicates architecture. The coordinator state machine and the agent-invocation layer each span multiple top-level modules with clear internal cohesion but no structural boundary, making dependency direction invisible and import discipline unenforceable. Existing circular-dependency workarounds (late imports, re-export chains) have accumulated rather than being resolved.

## Requirements

- The coordinator state machine and its supporting modules become a `theforge/coordinator/` package.
- The agent-invocation layer (subprocess, API, tool-runtime) becomes a `theforge/runners/` package.
- Each package exposes a small, intentional public API through `__init__.py`. No backward-compat re-export chains.
- The two new packages must not import from each other in either direction. Any shared types that currently create cross-boundary imports must be relocated so no circular dependencies remain.
- The top-level orchestration layer (`sprint.py`) and small stable modules (review, schemas, artifacts, task, sessions, traces) remain as top-level modules and may import from either package.
- No backward-compat re-export chains or compatibility shims introduced to preserve old import paths.
- 500 lines is an inspection threshold for modules. Splitting is driven by cohesion, not the number alone.

## Acceptance Criteria

- [ ] `theforge/coordinator/` and `theforge/runners/` exist as proper Python packages
- [ ] No circular imports between `coordinator/` and `runners/` — neither package imports from the other
- [ ] Any previously cross-boundary shared types are no longer imported across package lines
- [ ] No backward-compat re-export chains in any `__init__.py`
- [ ] `make test`, `make lint`, and `make gate` pass
- [ ] `forge.yaml` valid; all forge CLI commands behave identically
- [ ] No behavioral changes — pure structural refactor
