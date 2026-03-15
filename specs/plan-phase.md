---
name: "PLAN phase — implementation planning between PREFLIGHT and DEV"
slug: plan-phase
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/task.py
  - tests/test_coordinator.py
  - tests/test_task.py
pytest_target: tests/
---

# PLAN Phase

## Problem

The current state machine goes PREFLIGHT → DEV with no intermediate step. The
dev agent receives the spec and interprets it directly into code. When specs are
underspecified — missing edge cases, ambiguous API contracts, unclear ordering —
the dev agent makes assumptions. Reviewers catch the wrong assumptions. The agent
retries with different assumptions. This cycle repeats until budget is exhausted.

The manual practice that works: a discovery/planning session (Opus reads the
codebase and the spec, produces an exact implementation plan) before dev touches
any code.

## Solution

Add a `PLAN` phase between `PREFLIGHT` and `DEV`. A planning agent (opus profile)
reads the spec and file_scope contents, produces a structured `forge_plan.md`,
and the DEV agent receives the plan alongside the spec.

### State machine after this change

```
INIT → WORKSPACE → PREFLIGHT → PLAN → DEV → VALIDATE → REVIEW → DONE/ESCALATE
```

### When PLAN runs

PLAN runs when `preflight_complexity` is `"medium"` or `"large"`. Small complexity
specs skip PLAN entirely (direct PREFLIGHT → DEV, as today). If `plan_enabled` is
`False` in `forge.yaml`, PLAN is always skipped regardless of complexity.

### What the plan agent produces

The planning agent outputs a structured markdown document (`forge_plan.md`) saved
in the worktree root. It covers:

1. **Functions to add or modify** — exact names, signatures, docstrings
2. **Implementation order** — what to write first (dependencies)
3. **Edge cases** — each edge case with how to handle it
4. **Test scenarios** — specific test cases with inputs, expected outputs, mocks
5. **Risks / open questions** — anything ambiguous with a recommended resolution

The plan is NOT code. It is a structured handoff document.

### Plan output format

The planning agent must output ONLY the plan document. No prose before or after.
It starts with `# Implementation Plan` and is valid markdown.

Example structure:
```
# Implementation Plan: <task name>

## Summary
One paragraph: what we're implementing and why.

## Implementation Order
1. Step one — why first
2. Step two — depends on step one
...

## Functions to Modify

### `module.function_name(args) -> return_type`
- **File**: `src/theforge/module.py`
- **Change**: Add parameter X, handle edge case Y
- **Signature**: `def function_name(a: str, b: int = 0) -> bool:`

## Edge Cases

| Condition | Expected Behavior | Notes |
|-----------|-------------------|-------|
| Empty list passed | Return [] immediately | No error |
| ...       | ...               | ...   |

## Test Scenarios

### `test_scenario_name`
- **Setup**: mock X returns Y
- **Call**: `function_name("input")`
- **Assert**: returns True, log contains "message"

## Risks and Ambiguities

- **Risk**: The spec says X but the code does Y — resolve by doing Z
```

### Plan agent prompt: `build_plan_prompt()` in task.py

```python
def build_plan_prompt(
    task: TaskSpec,
    *,
    spec_content: str,
    file_contents: dict[str, str],
    preflight_output: str | None = None,
) -> str:
```

The prompt instructs the agent to:
- Read the spec and file contents
- NOT write any code — planning only
- Output ONLY the plan document (starts with `# Implementation Plan`)
- Cover all required sections

### CoordinatorState additions

```python
plan_result: AgentResult | None = None
plan_output: str | None = None  # contents of forge_plan.md, passed to dev
```

`total_cost` property already sums dev+review+preflight; add plan to it:
```python
@property
def total_plan_cost(self) -> float:
    return self.plan_result.cost_usd if self.plan_result else 0.0

@property
def total_cost(self) -> float:
    return self.total_dev_cost + self.total_review_cost + self.total_preflight_cost + self.total_plan_cost
```

### Coordinator changes (coordinator.py)

1. Add `Phase.PLAN = auto()` to the `Phase` enum (between PREFLIGHT and DEV)

2. After the PREFLIGHT phase returns PROCEED, check if PLAN should run:
   ```python
   if should_plan:
       state.phase = Phase.PLAN
       # run plan agent
       # save forge_plan.md to workspace_path
       # store plan_output in state
   ```

3. The PLAN agent is invoked via `run_agent()` with the plan profile.

4. If the PLAN agent fails (non-zero exit), log a warning and proceed to DEV
   without a plan (fail-open, not fail-closed). Log:
   `"⚠ PLAN failed — proceeding to DEV without plan"`

5. `should_plan` logic:
   ```python
   should_plan = (
       config.plan.enabled
       and state.preflight_complexity in ("medium", "large")
   )
   ```

6. On DEV **retry** (review sends REQUEST_CHANGES), do NOT re-run PLAN. Pass the
   original `state.plan_output` to `build_dev_prompt()` on every iteration.

### build_dev_prompt() addition

Add `plan_output: str | None = None` parameter. When present, inject before the
spec section:

```
## Implementation Plan (from planning agent)

The planning agent has already analysed this codebase and produced a detailed
implementation plan. Follow it closely — do not re-derive the approach from scratch.

{plan_output}
```

### forge.yaml config additions

```yaml
plan:
  enabled: true          # set to false to skip PLAN phase entirely
  model: claude          # profile name (defaults to "claude" which uses opus)
  budget_usd: 0.50       # per-plan budget
  timeout: 300           # seconds
```

Add a `plan` profile to `forge.yaml` dev_profiles or use the existing `claude`
profile. The plan agent uses `claude-opus-4-5` (or the configured model) because
planning is an architectural reasoning task.

### Config dataclass additions (config.py)

```python
@dataclass
class PlanConfig:
    enabled: bool = True
    model: str = "claude"
    budget_usd: float = 0.50
    timeout: int = 300

@dataclass
class ForgeConfig:
    ...
    plan: PlanConfig = field(default_factory=PlanConfig)
```

## Acceptance Criteria

- [ ] `Phase.PLAN` added to Phase enum (between PREFLIGHT and DEV)
- [ ] `plan_result: AgentResult | None = None` added to CoordinatorState
- [ ] `plan_output: str | None = None` added to CoordinatorState
- [ ] `total_plan_cost` property added to CoordinatorState
- [ ] `total_cost` property includes plan cost
- [ ] `PlanConfig` dataclass added to config.py with `enabled`, `model`, `budget_usd`, `timeout`
- [ ] `ForgeConfig` has `plan: PlanConfig` field
- [ ] `build_plan_prompt()` added to task.py with correct signature
- [ ] `build_plan_prompt()` prompt instructs agent to output ONLY plan markdown
- [ ] `build_dev_prompt()` accepts `plan_output: str | None = None` parameter
- [ ] When `plan_output` is provided, `build_dev_prompt()` includes it before the spec
- [ ] PLAN phase runs after PREFLIGHT PROCEED when complexity is medium or large
- [ ] PLAN phase is skipped when complexity is small
- [ ] PLAN phase is skipped when `config.plan.enabled` is False
- [ ] PLAN agent failure is logged as warning and execution continues to DEV (fail-open)
- [ ] `forge_plan.md` is written to workspace root when plan succeeds
- [ ] `state.plan_output` is set to the plan text on success
- [ ] On DEV retry, PLAN does not re-run — original `state.plan_output` is reused
- [ ] forge.yaml at project root has `plan:` section with `enabled: true`
- [ ] Existing tests pass without modification
- [ ] New test: complexity=medium → PLAN runs before DEV
- [ ] New test: complexity=small → PLAN skipped, goes directly to DEV
- [ ] New test: plan.enabled=false → PLAN always skipped
- [ ] New test: PLAN agent fails → warning logged, DEV runs without plan
- [ ] New test: `build_plan_prompt()` returns prompt containing file contents
- [ ] New test: `build_dev_prompt()` with plan_output includes plan section
- [ ] New test: `build_dev_prompt()` without plan_output omits plan section
