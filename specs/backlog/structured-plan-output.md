---
name: "Structured plan output — YAML steps with spec-requirement mapping"
slug: structured-plan-output
pytest_target: tests/
---

# Structured Plan Output — YAML Steps with Spec-Requirement Mapping

## Problem

The PLAN phase produces freeform markdown (`forge_plan.md`). This causes
four concrete problems:

1. **No mechanical validation** — the coordinator can't check if the plan
   covers all acceptance criteria from the spec. Coverage gaps only surface
   during review, after expensive dev cycles.
2. **No structured handoff to DEV** — the dev agent gets a blob of markdown
   and has to parse intent. Step boundaries, file targets, and risk levels
   are implicit.
3. **Plan review is qualitative only** — reviewers assess prose, not
   structure. There's no way to mechanically verify that every AC is
   addressed by at least one plan step.
4. **No step-level progress tracking** — can't tell which plan steps the
   dev agent completed vs. skipped vs. partially implemented.

## Solution

Define a structured plan format that is YAML with markdown content, not pure
freeform markdown. The coordinator validates the plan mechanically before
handing off to DEV. All validation is deterministic Python — no LLM in the
loop.

### Plan format

The planning agent outputs a fenced YAML block in its response:

```yaml
overview: |
  One-paragraph summary of the approach.

steps:
  - id: 1
    description: "Add FindingRecord dataclass to coord_state.py"
    files:
      - src/theforge/coord_state.py
      - tests/test_coord_state.py
    criteria_covered:
      - "FindingRecord dataclass in coord_state.py with all specified fields"
    risk: low
    notes: "Straightforward dataclass addition, no existing code changes"

  - id: 2
    description: "Wire cycle_history into build_review_prompt()"
    files:
      - src/theforge/task.py
      - tests/test_task.py
    criteria_covered:
      - "build_review_prompt() accepts cycle_history parameter"
      - "Cycle 2+ review prompts include previous cycle findings"
    risk: medium
    notes: "Need to handle cycle 1 vs cycle 2+ prompt divergence"

risks:
  - description: "Large diff in task.py may trigger merge conflicts with concurrent work"
    mitigation: "Keep changes isolated to new functions"

uncovered_criteria: []  # AC items not addressed by any step — should be empty
```

#### Field definitions

- `overview` — free-text summary, rendered as-is in dev prompt
- `steps` — ordered list; each step has:
  - `id` — integer, sequential from 1
  - `description` — what the step does (one sentence)
  - `files` — list of files the step expects to touch
  - `criteria_covered` — list of acceptance criteria strings this step addresses
  - `risk` — `low | medium | high`
  - `notes` — optional context for the dev agent
- `risks` — top-level risks with mitigations (may be empty list)
- `uncovered_criteria` — AC items not addressed by any step; should be
  empty for a complete plan

### Mechanical validation (coordinator, not LLM)

After the PLAN agent produces the structured output, the coordinator runs
deterministic validation. No LLM is involved in any of these checks.

1. **Parse YAML** — extract the fenced `yaml` block from agent output,
   parse with `yaml.safe_load()`
2. **AC coverage** — collect all `criteria_covered` strings across steps,
   compare against spec acceptance criteria. Flag any spec AC not covered
   by at least one step.
3. **Uncovered criteria check** — if `uncovered_criteria` is non-empty,
   surface as a validation finding (the plan agent is explicitly flagging
   gaps).
4. **No empty file lists** — every step must have at least one entry in
   `files`. A step that touches nothing is not a real step.
5. **Step ID uniqueness and ordering** — IDs must be unique integers,
   sequential from 1.

Validation failures → plan regen (if plan review is enabled and regen is
available) or surfaced as plan review findings. The coordinator does not
silently swallow validation errors.

### Plan prompt changes

`build_plan_prompt()` in `task.py` is updated to instruct the planning
agent to output structured YAML in a ` ```yaml ` fenced block. The prompt
includes the schema definition and a concrete example. The agent is told
to list every acceptance criterion from the spec in at least one step's
`criteria_covered` field.

### Dev prompt changes

`build_dev_prompt()` in `task.py` renders the structured plan as an ordered
task list with file targets per step. Format:

```
## Implementation Plan

### Step 1: Add FindingRecord dataclass to coord_state.py
Files: src/theforge/coord_state.py, tests/test_coord_state.py
Risk: low
Notes: Straightforward dataclass addition, no existing code changes

### Step 2: Wire cycle_history into build_review_prompt()
Files: src/theforge/task.py, tests/test_task.py
Risk: medium
Notes: Need to handle cycle 1 vs cycle 2+ prompt divergence
```

The dev agent gets a clearer roadmap than a freeform document. File targets
per step let the agent focus its work.

### Backward compatibility

If the plan output doesn't parse as valid structured YAML (e.g., older plan
agents, fallback models, or malformed output), treat the entire output as
freeform markdown with a warning log:

```
[forge]   ⚠ Plan output is not structured YAML — falling back to freeform markdown
```

The coordinator continues with the freeform plan. No validation checks run
in fallback mode. This ensures existing flows and older plan agents are not
broken.

### Plan review integration

When plan review is enabled (see `plan-review.md`), the plan reviewer sees
the structured format. Reviewers can:

- Mechanically verify criteria coverage (the coordinator already flags gaps)
- Reference plan steps by ID in findings (e.g., "Step 3 underestimates risk")
- Check that `uncovered_criteria` is empty

The `criteria_coverage` field in plan review output maps 1:1 to the plan's
`criteria_covered` entries.

## Implementation scope

### New module: `src/theforge/plan.py`

Contains:
- `parse_structured_plan(raw_output: str) -> StructuredPlan | None` — extracts
  and parses YAML from agent output, returns `None` on parse failure
- `validate_plan(plan: StructuredPlan, spec_criteria: list[str]) -> list[PlanFinding]`
  — runs all mechanical checks, returns list of findings
- `render_plan_for_dev(plan: StructuredPlan) -> str` — renders structured plan
  as markdown task list for dev prompt
- `StructuredPlan` dataclass — typed representation of the plan YAML
- `PlanStep` dataclass — typed representation of a single step
- `PlanFinding` dataclass — validation finding (severity, message)

### Changes to `src/theforge/task.py`

- `build_plan_prompt()` — add structured YAML schema and instructions to prompt
- `build_dev_prompt()` — if plan is structured, render via `render_plan_for_dev()`;
  otherwise render freeform markdown as before

### Changes to `src/theforge/coordinator.py`

- After PLAN agent completes, attempt `parse_structured_plan()` on output
- If structured plan parsed, run `validate_plan()` and store findings
- Validation findings feed into plan review (if enabled) or log warnings
- Store `StructuredPlan` in `CoordinatorState` for dev prompt rendering

### Changes to `src/theforge/coord_state.py`

- Add `structured_plan: StructuredPlan | None = None` to state
- Add `plan_validation_findings: list[PlanFinding] = field(default_factory=list)`

## Acceptance Criteria

- [ ] Plan prompt instructs agent to output structured YAML format with
      schema and example in the prompt
- [ ] Plan output parsed as YAML by coordinator via `parse_structured_plan()`
- [ ] Mechanical validation: all spec AC covered by at least one step's
      `criteria_covered`
- [ ] Mechanical validation: no empty `files` lists in steps
- [ ] Mechanical validation: `uncovered_criteria` non-empty flagged as finding
- [ ] Mechanical validation: step IDs unique and sequential
- [ ] Validation failures surfaced as plan review findings or trigger regen
- [ ] Dev prompt renders structured plan as ordered task list with file
      targets per step
- [ ] Freeform markdown fallback with warning when YAML parsing fails
- [ ] Plan review can reference plan steps by ID
- [ ] Existing tests pass (freeform plans still work via fallback)
- [ ] New tests for structured plan YAML parsing (valid input)
- [ ] New tests for structured plan YAML parsing (malformed input → fallback)
- [ ] New tests for AC coverage validation (full coverage, partial, none)
- [ ] New tests for empty file list validation
- [ ] New tests for `render_plan_for_dev()` output format
- [ ] New tests for backward compatibility with freeform markdown plans

## Dependencies

- `plan-review.md` — plan review integration benefits from structured format
  but is not required. Structured plan validation works independently.

## Future

- **Step-level progress tracking** — once the plan is structured, the
  coordinator can track which steps the dev agent completed by diffing
  committed files against step `files` lists. This is a separate spec.
- **Plan diff on regenerate** — when a plan is regenerated, show the
  structural diff (added/removed/changed steps) rather than a text diff.
- **Criteria coverage scoring** — surface a coverage percentage in audit
  output for plan quality metrics.
