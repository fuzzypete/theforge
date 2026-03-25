---
name: "Decompose oversized modules within coordinator package"
slug: "refactor-coordinator-internal-split"
pytest_target: tests/
---

# Decompose oversized modules within coordinator package

## Problem

After the coordinator/ package is formed, several internal modules remain oversized: `coord_phases.py` (1436 lines) contains all phase handlers interleaved, `coord_notify.py` (927 lines) mixes notification dispatch with plan review interaction, and the main coordinator module still contains the entry points, loop logic, and miscellaneous helpers in one place. These sizes make it hard to understand what a module owns and where new phase-related code should go.

## Requirements

- Split oversized modules within `coordinator/` along cohesion boundaries. Phase handlers that are independently testable should be independently locatable.
- Notification dispatch and human-review interaction should be separable concerns.
- The coordinator loop and public entry points (`run_task`, `run_from_review`, `run_from_dev`, `run_review_only`) should be in a module that is clearly the "front door" without also containing 1000 lines of helpers.
- No module within `coordinator/` should exceed 500 lines without cohesion justification.
- The `coordinator/` public API (exposed through `__init__.py`) must not change.

## Acceptance Criteria

- [ ] No module within `coordinator/` exceeds 500 lines without documented justification
- [ ] Phase handlers are individually locatable without reading an interleaved 1400-line file
- [ ] `coordinator/__init__.py` public API unchanged
- [ ] `make test`, `make lint`, and `make gate` pass
- [ ] No behavioral changes
