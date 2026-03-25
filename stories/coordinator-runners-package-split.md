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
- Dependency direction is strictly layered: `sprint → coordinator → runners`. No reverse edges, no circular imports.
- Any shared types that currently create cross-boundary imports are relocated to break the cycles.
- Sprint and small stable modules (review, schemas, artifacts, task, sessions, traces) remain as top-level modules.
- Big-bang migration: all moves and import updates in one pass. No compatibility shims. The test suite is the safety net.
- 500 lines is an inspection threshold for modules. Splitting is driven by cohesion, not the number alone.

## Acceptance Criteria

- [ ] `theforge/coordinator/` and `theforge/runners/` exist as proper Python packages
- [ ] No circular imports between packages; dependency graph enforced by a boundary test
- [ ] No backward-compat re-export chains in any `__init__.py`
- [ ] `make test`, `make lint`, and `make gate` pass
- [ ] `forge.yaml` valid; all forge CLI commands behave identically
- [ ] No behavioral changes — pure structural refactor
