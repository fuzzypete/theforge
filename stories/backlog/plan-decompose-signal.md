---
name: "PLAN signals DECOMPOSE_NEEDED — escalate oversized stories before DEV"
slug: plan-decompose-signal
pytest_target: tests/
---

# PLAN signals DECOMPOSE_NEEDED

## Problem

Stories that are too large for a single dev pass waste cycles. The planner
can see this — it knows how many files, how many ACs, how complex the
changes are — but has no way to signal "stop, this needs to be split."

Observed: config-normalization combined field unification + validation
hardening into one story. The dev agent timed out at 900s, broke 6 tests
from validation changes that conflicted with existing test configs, and
burned 3 iterations without converging. A planner seeing "modify 5+ parse
paths in a 1184-line file AND add 5 new validation errors AND update all
tests" should have flagged this before DEV started.

## Solution

The plan phase gains a `decompose` signal. When the planner determines the
story scope exceeds what a single dev pass can handle, it outputs:

```yaml
decompose: true
reason: "Story requires 6+ file modifications across config parsing,
  validation, and test updates. Recommend splitting into field
  normalization and validation hardening."
suggested_splits:
  - "Field unification: plan.model_name → model, plan.cli"
  - "Validation hardening: ConfigError on bad combinations"
```

### Coordinator behavior

When the plan output contains `decompose: true`:

1. Log the reason and suggested splits clearly
2. Set state to ESCALATE with `escalation_reason = "DECOMPOSE_NEEDED"`
3. Notify via configured channels (ntfy, slack, etc.)
4. Do NOT proceed to DEV — the story needs manual intervention

This is NOT auto-decomposition. The coordinator does not create sub-stories
or modify specs. A human (or a future auto-decompose system) reads the
planner's suggestions and decides how to split.

### Planner prompt addition

The plan prompt gains guidance:

> If the implementation requires modifying more than 5 files substantially,
> or if the acceptance criteria span two or more independent concerns that
> could ship separately, signal `decompose: true` with a reason and
> suggested splits. Do not produce a plan for work that should be split.

### When NOT to signal decompose

- Many files touched but all in the same concern (e.g., a rename across 10 files)
- Large test surface but small implementation (adding tests for existing code)
- The story explicitly says "bundle" or "batch" in its description

## Acceptance criteria

- Plan output schema accepts `decompose`, `reason`, `suggested_splits`
- `decompose: true` in plan output → coordinator escalates (no DEV)
- Escalation notification includes the planner's reason and splits
- `decompose: false` or absent → normal flow (backward compat)
- Planner prompt includes decomposition guidance
- All existing tests pass
- New tests: decompose signal triggers escalation, absent field is no-op
