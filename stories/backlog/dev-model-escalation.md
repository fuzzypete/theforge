---
name: "dev model escalation on persistent P1s"
slug: dev-model-escalation
file_scope:
  - src/theforge/config.py
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Dev Model Escalation (Smart Config Level 3)

## Problem

When a P1 finding persists across review cycles, the dev agent is stuck.
Today, forge retries with the same model up to `max_review_cycles` times
and then escalates to a human. But the model might just be unable to see
the integration gap — a stronger or different model might fix it immediately.

Real example: gate-override spec ran 5 review cycles. Every cycle, opus and
codex reviewers flagged the same P1 ("cli.py never wires gate_override into
TaskSpec"). Sonnet as dev fixed P2s each iteration but never addressed the
P1. A two-line fix that a human saw instantly.

## Design

### Persistent P1 detection

After each review cycle, the coordinator compares the current P1 findings
against the previous cycle's P1 findings. A P1 is "persistent" if a
finding with similar description and file appears in consecutive cycles.

Similarity is determined by:
1. Same `file` field
2. Description overlap (fuzzy match — substring containment or >60% token
   overlap)

If any P1 persists across 2 consecutive cycles, trigger escalation.

### Model escalation

When persistent P1 is detected and smart config is active (`models` key):

1. Look up current dev model in `MODEL_REGISTRY`
2. Find eligible models with higher `capability` score
3. Filter to `dev_capable=True` models only (excludes gemini for now)
4. Select the next one up by capability
5. Swap the dev profile for the next iteration
6. Log clearly: `[forge]   Dev escalation: sonnet → opus (persistent P1)`

If no higher model is available (already using the strongest), continue
with current model — budget remains the safety valve.

### Dev-capable flag

`ModelInfo` gains a `dev_capable: bool` field:

```python
MODEL_REGISTRY = {
    "claude/sonnet":       ModelInfo(..., dev_capable=True),
    "claude/opus":         ModelInfo(..., dev_capable=True),
    "openai/gpt-5.4":      ModelInfo(..., dev_capable=True),
    "google/gemini-2.5-pro": ModelInfo(..., dev_capable=False),
}
```

This is separate from `tier` — a model can be "strong" for review but not
capable enough for dev work (or its CLI doesn't support the required tools
for development).

### Escalation budget

The escalated model uses the remaining dev budget (not a fresh allocation).
If the dev budget is exhausted, no escalation occurs.

---

## Requirements

### R1: Persistent P1 detection

```python
def _has_persistent_p1(
    current_findings: list[dict],
    previous_findings: list[dict],
) -> bool:
    """Return True if any P1 appears in both current and previous cycles."""
```

Compare P1 findings between consecutive review cycles. Match on file +
description similarity (substring containment).

### R2: `dev_capable` field on ModelInfo

Add `dev_capable: bool = True` to `ModelInfo`. Set to `False` for models
whose CLI doesn't support dev tools or whose output quality isn't
sufficient for code generation.

### R3: Model escalation function

```python
def _escalate_dev_model(
    current_model: str,
    available_models: list[str],
) -> str | None:
    """Return the next higher-capability dev-capable model, or None."""
```

### R4: Coordinator integration

In the review loop, after parsing review verdict as REQUEST_CHANGES:

1. Check `_has_persistent_p1()` against previous cycle's findings
2. If persistent AND smart config active → call `_escalate_dev_model()`
3. If escalation available → swap `dev_profile` for next iteration
4. Log the escalation decision

### R5: Escalation limits

- Maximum 1 escalation per run (don't cascade sonnet → opus → ?)
- Only when `models` key is present (smart config mode)
- Only on persistent P1s (not new P1s)
- Escalation does NOT reset the review cycle counter

### R6: CLI display

```
[forge] ▸ REVIEW   cycle=2
[forge]   ✗ REVIEW   REQUEST_CHANGES  1 P1 (persistent)
[forge]   Dev escalation: sonnet → opus (persistent P1 in cli.py)
[forge] ▸ DEV   opus  iter=1
```

### R7: Tests

- `test_persistent_p1_detected`: same P1 across cycles → detected
- `test_new_p1_not_persistent`: different P1s → not detected
- `test_escalation_swaps_dev_model`: persistent P1 → dev model upgraded
- `test_escalation_skips_non_dev_capable`: gemini not selected for dev
- `test_escalation_max_once`: only one escalation per run
- `test_escalation_only_with_smart_config`: no escalation without `models` key
- `test_escalation_no_higher_model`: already on strongest → no swap
- `test_p1_similarity_matching`: fuzzy match on file + description

---

## Acceptance Criteria

1. Persistent P1 across 2+ review cycles triggers dev model escalation
2. Escalation selects next higher dev-capable model from registry
3. Gemini (or other non-dev-capable models) are never selected for dev
4. Maximum 1 escalation per run
5. Only active when smart config (`models` key) is used
6. Clear logging of escalation decisions
7. All existing tests pass unchanged

## Dependencies

- `smart-model-config` must land first (provides `ModelInfo`, `MODEL_REGISTRY`)

## Out of Scope

- Multi-step escalation chains (sonnet → codex → opus in one run)
- Escalation based on gate failures (only review P1s)
- De-escalation (dropping to cheaper model after success)
- Cross-run learning (remembering which models work for which spec types)
