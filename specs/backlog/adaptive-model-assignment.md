---
name: "Adaptive model assignment — complexity-driven routing with escalation learning"
slug: adaptive-model-assignment
pytest_target: tests/
depends_on: [smart-defaults, api-mode-dev]
---

# Adaptive Model Assignment

## Problem

Today every story gets the same dev model, same review pool, same
timeouts. But a 5-AC React component story and a 12-AC cross-module
refactor story shouldn't run the same way. The human shouldn't have
to think about which model goes where — declare what's available and
let the system match models to stories based on evidence.

The preflight already classifies complexity (LOW/MEDIUM/HIGH). API-mode
dev agents are now live. Smart defaults establish the right baseline
tiering. The missing piece is the coordinator logic that reads the pool,
reads the preflight signal, and makes the assignment.

## Design Principles

1. **Deterministic** — same story + same pool + same complexity = same
   assignment. No LLM in the loop for routing.
2. **Explicit overrides win** — if forge.yaml names a specific dev profile,
   adaptive assignment is skipped for that role.
3. **Evidence-based escalation** — track which model/complexity combinations
   escalate, and auto-promote on repeated failures.
4. **Cheap by default** — start with the cheapest capable model and
   escalate up, not the other way around.

## Config Format

```yaml
agents:
  # The available pool. Adaptive assignment picks from these.
  - name: haiku
    provider: anthropic
    model: haiku
    budget_usd: 1.00
    timeout_seconds: 300
    tier: cheap              # cheap | mid | strong
    strengths: [fast, triage]

  - name: sonnet
    provider: anthropic
    model: sonnet
    budget_usd: 5.00
    timeout_seconds: 900
    tier: mid
    strengths: [code, tools, general]

  - name: opus
    provider: anthropic
    model: opus
    budget_usd: 8.00
    timeout_seconds: 1200
    tier: strong
    strengths: [architecture, review, complex-logic]

  - name: deepseek-r1
    provider: deepseek
    model: deepseek-reasoner
    budget_usd: 1.00
    timeout_seconds: 600
    tier: strong
    strengths: [reasoning, review, math]

  - name: gpt4o
    provider: openai
    model: gpt-4o
    budget_usd: 3.00
    timeout_seconds: 600
    tier: mid
    strengths: [code, tools, general]

assignment:
  enabled: true                  # false = use explicit profiles only
  min_reviewers: 1
  max_reviewers: 3
  prefer_cross_provider: true    # diversify review pools across providers
  budget_per_story_usd: 15.00   # max spend cap per story
  escalation_memory: true        # learn from past escalations
```

## Assignment Logic

### Phase → Tier Mapping (default)

| Phase        | LOW complexity | MEDIUM complexity | HIGH complexity |
|-------------|---------------|-------------------|-----------------|
| Preflight   | cheap          | cheap              | cheap           |
| Plan        | mid            | strong             | strong          |
| Plan Review | mid            | strong (x2)        | strong (x2-3)   |
| Dev         | cheap          | mid                | strong          |
| Code Review | mid (x1)       | strong (x2)        | strong (x2-3)   |

### Reviewer Pool Selection

1. Select `n` reviewers based on complexity (min_reviewers..max_reviewers)
2. If `prefer_cross_provider`, ensure no two reviewers share a provider
   (when possible given the pool)
3. Prefer `strong` tier for review roles regardless of complexity
4. Break ties by: lowest cost among same tier

### Escalation Learning

Stored in `.forge/assignment_history.yaml`:

```yaml
escalations:
  - story: fix-auth-bug
    complexity: MEDIUM
    dev_model: sonnet
    outcome: ESCALATE
    reason: "max review cycles exceeded"
    timestamp: 2026-03-15T10:30:00Z

  - story: add-search-filter
    complexity: MEDIUM
    dev_model: sonnet
    outcome: DONE
    timestamp: 2026-03-15T11:00:00Z
```

Rules (deterministic, not LLM):
- If a complexity tier has escalated **2+ times in the last 10 runs** with
  the current dev model, auto-promote to the next tier.
- Auto-promotion is logged: `"[adaptive] MEDIUM dev promoted sonnet → opus
  (2/8 recent MEDIUM stories escalated with sonnet)"`
- Auto-promotion is sticky for the sprint (resets between sprints).
- Manual override (`profiles.dev` explicit) always wins.

### Preflight Domain Tags (future extension)

Preflight output gains:
```yaml
complexity: medium
domains: [react, zustand, css]
estimated_files: 4
```

Domain tags enable future refinements:
- Prefer models with matching `strengths` for domain-specific reviews
- Drop irrelevant tools (no Bash for pure docs stories)
- Adjust timeouts based on estimated_files

This is additive — assignment works without domains using complexity alone.

## Implementation

### New module: `src/theforge/assignment.py`

```python
@dataclass
class AssignmentDecision:
    preflight: ModelProfile
    planner: ModelProfile
    plan_reviewers: list[ModelProfile]
    dev: ModelProfile
    code_reviewers: list[ModelProfile]
    rationale: dict[str, str]  # phase → reason string for logging

def assign_models(
    agents: list[AgentDef],
    assignment_config: AssignmentConfig,
    complexity: str,  # LOW | MEDIUM | HIGH
    escalation_history: list[EscalationRecord] | None = None,
    explicit_profiles: dict[str, ModelProfile] | None = None,
) -> AssignmentDecision:
    """Pure deterministic function. No LLM. No I/O."""
```

### Coordinator integration

In `_phase_plan()` and `_phase_dev()`, check if adaptive assignment is
enabled. If so, call `assign_models()` after preflight and use the
returned profiles instead of the static ones from config.

### Logging

Every assignment decision logged at verbose:
```
[adaptive] Complexity: MEDIUM (from preflight)
[adaptive] Dev: sonnet (mid tier, $5.00 budget, 900s timeout)
[adaptive] Plan: opus (strong tier — high leverage phase)
[adaptive] Review pool: [opus, deepseek-r1] (2 reviewers, cross-provider)
[adaptive] Escalation history: 0/5 recent MEDIUM escalated — no promotion needed
```

## Acceptance Criteria

- `agents:` key in forge.yaml defines the available model pool with tier + strengths
- `assignment:` key configures bounds and behavior
- `assign_models()` is a pure function — deterministic, no LLM, no I/O
- Complexity LOW/MEDIUM/HIGH maps to tier selection per the table above
- Review pools prefer cross-provider diversity when pool allows
- Explicit `profiles:` config overrides adaptive for that specific role
- Escalation history tracked in `.forge/assignment_history.yaml`
- Auto-promotion fires after 2+ escalations in last 10 runs for a tier
- Auto-promotion logged with clear rationale
- Auto-promotion is sticky per sprint, resets between sprints
- Budget cap (`budget_per_story_usd`) enforced — downgrade model before exceeding
- All assignment decisions logged at verbose level
- Deterministic: same inputs = same outputs every time
- Tests cover: tier selection per complexity, cross-provider preference,
  escalation promotion, explicit override, budget cap enforcement
