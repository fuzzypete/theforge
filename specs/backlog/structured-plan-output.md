---
name: "Structured plan output — YAML step-by-step format"
slug: structured-plan-output
pytest_target: tests/
---

# Structured Plan Output

## Problem

Plan output is freeform markdown. This creates two downstream problems:

1. **Cheap/local models can't follow it** — a markdown plan with prose context
   requires strong reasoning to extract the actual steps. A structured YAML
   plan with explicit file targets, step ordering, and AC mapping can be
   followed mechanically.

2. **Plan validation is impossible** — without structure, there's no way to
   mechanically verify that a plan covers all acceptance criteria, references
   valid files, or has concrete steps. Plan review is entirely LLM-based.

## Solution

Change the plan agent's output format from freeform markdown to structured
YAML. The coordinator parses the YAML and can validate it mechanically before
spending dev budget.

### Plan format

\`\`\`yaml
plan:
  approach: "<1-2 sentence summary of the implementation strategy>"
  steps:
    - id: 1
      description: "Add FindingRecord dataclass to coord_state.py"
      files:
        - src/theforge/coord_state.py
      action: modify  # modify | create | delete
      details: "Add frozen dataclass with fields: finding_id, cycle_first_seen, ..."
    - id: 2
      description: "Create finding_classifier.py"
      files:
        - src/theforge/finding_classifier.py
      action: create
      details: "Implement fingerprint matching and disposition classification..."
      depends_on: [1]
  criteria_mapping:
    - criterion: "FindingRecord dataclass in coord_state.py"
      steps: [1]
    - criterion: "Deterministic fingerprint matching"
      steps: [2]
  risks:
    - description: "Jaccard threshold may need tuning"
      mitigation: "Parameterize threshold, default 0.5"
\`\`\`

### Key properties

- **steps** have explicit file targets, action type, and optional dependencies
- **criteria_mapping** traces every AC to the step(s) that address it
- **risks** capture known uncertainties (currently lost in prose)
- **depends_on** allows the coordinator to detect step ordering issues

### Backward compatibility

The plan agent prompt asks for YAML output. If the agent returns markdown
(old behavior or model doesn't follow instructions), the coordinator falls
back to treating it as freeform — no crash, just loses the structured benefits.

Detection: check if output starts with \`plan:\` or \`---\` followed by \`plan:\`.

### Dev prompt integration

\`build_dev_prompt()\` renders the structured plan as a clear step-by-step
checklist instead of injecting raw markdown. Each step becomes:

\`\`\`
Step 1: Add FindingRecord dataclass to coord_state.py
  Action: modify
  Details: Add frozen dataclass with fields: finding_id, cycle_first_seen, ...

Step 2: Create finding_classifier.py
  Action: create
  Depends on: Step 1
  Details: Implement fingerprint matching and disposition classification...
\`\`\`

### Plan review integration

\`build_plan_review_prompt()\` includes the criteria_mapping so reviewers can
verify coverage mechanically rather than reading prose and guessing.

## Acceptance Criteria

- [ ] Plan agent prompt updated to request YAML output format
- [ ] Plan output parsed as YAML in coordinator; stored as structured dict
- [ ] Fallback: freeform markdown still accepted (no crash on non-YAML output)
- [ ] \`build_dev_prompt()\` renders structured plan as step-by-step checklist
- [ ] \`build_plan_review_prompt()\` includes criteria_mapping for reviewers
- [ ] Plan dataclass or TypedDict defined for type safety
- [ ] Steps have: id, description, files, action, details, optional depends_on
- [ ] criteria_mapping traces ACs to step IDs
- [ ] Plan stored in coordinator state for audit trail
- [ ] All existing tests pass
- [ ] New tests for YAML plan parsing, fallback to markdown, dev prompt rendering
