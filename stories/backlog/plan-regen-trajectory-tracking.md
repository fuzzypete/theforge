---
name: "Plan regen: track per-attempt metadata for convergence detection"
slug: plan-regen-trajectory-tracking
pytest_target: tests/test_coord_plan.py
---

# Plan regen: per-attempt metadata tracking

## Problem

When a plan is rejected multiple times, the coordinator has no memory of prior
attempts beyond the latest findings. It cannot distinguish "new problem" from
"same problem, different words." It cannot detect that complexity is growing
or that the same issue family is surviving across attempts.

## Expected behavior

`CoordinatorState` tracks per-attempt plan metadata. After each plan review,
the coordinator computes:

- **files_touched**: count of files the plan proposes to modify (extractable
  from plan structure)
- **finding_count**: P1 and P2 counts from the merged review
- **finding_themes**: deduped structural markers extracted from finding text —
  function names, parameter names, and file paths mentioned in descriptions
  (e.g. `strict_auth`, `load_config`, `_validate_plan_provider`)

From this history the coordinator mechanically classifies each rejection:

- **patch**: no finding themes from attempt N-1 appear in attempt N, or
  finding count is decreasing
- **backtrack**: one or more finding themes from attempt N-1 survive into
  attempt N unchanged, AND plan complexity (files_touched) is flat or growing
- **escalate**: a backtrack attempt was already made and the same theme
  survives

The classification is stored on state and consumed by the regen prompt builder.

## Acceptance criteria

- `CoordinatorState` has `plan_attempt_metadata: list[dict]` — one entry per
  attempt with `files_touched`, `p1_count`, `p2_count`, `finding_themes`
- Finding themes are extracted using the structural anchor model defined in
  `plan-finding-identity`: multi-segment snake_case/camelCase identifiers and
  dotted paths from finding `description` fields; file paths are valid anchors
  but not sufficient alone — no LLM call
- After each review merge, the coordinator appends metadata and computes
  `plan_regen_disposition: "patch" | "backtrack" | "escalate"`
- Disposition is logged at the plan review step
- All existing tests pass
- New tests cover theme extraction and disposition classification across
  synthetic attempt sequences
