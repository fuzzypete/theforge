---
name: Assignment history end-to-end fixture
slug: assignment-history-end-to-end
pytest_target: tests/
depends_on: [adaptive-model-assignment, escalation-learning]
---

# Assignment History End-to-End Fixture

## Problem

The "learning" part of adaptive assignment is the least proven part of the
system. The history file exists and the pure promotion logic has unit coverage,
but there is not yet a small end-to-end fixture that proves the full history
loop behaves correctly over multiple runs.

To dogfood adaptive assignment on TheForge itself, we need a durable test that
proves the coordinator can read history, promote on repeated failures, keep the
promotion sticky within a sprint, and append the final run record correctly.

## Acceptance criteria

- Add a small end-to-end fixture for `.forge/assignment_history.yaml`
- Test empty history: no promotion, no error
- Test two prior escalations for the same complexity/dev-model pair: next run
  promotes
- Test sprint stickiness: once promotion is triggered for the sprint, later
  matching runs in that sprint reuse it
- Test final record append: the completed run is appended with the expected
  story, complexity, dev model, outcome, and timestamp fields
- Test both `DONE` and `ESCALATE` outcomes in the fixture coverage
- Tests exercise coordinator integration, not only pure helper functions
