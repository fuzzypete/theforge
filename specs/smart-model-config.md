---
name: "smart model config: declarative model pool with complexity-adaptive assignment"
slug: smart-model-config
file_scope:
  - src/theforge/config.py
  - src/theforge/coordinator.py
  - src/theforge/runner.py
  - src/theforge/task.py
  - tests/test_config.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Smart Model Config: Declarative Model Pool with Complexity-Adaptive Assignment

## Problem

The current `forge.yaml` requires operators to manually assign specific models to
specific stages (`dev`, `review_pool`, `synthesis`, `preflight`). This creates
two problems:

1. **Premature commitment**: You're choosing which model writes code vs. reviews
   code before you know what the task looks like. A config tweak doesn't need
   Opus for dev, but a cross-cutting refactor might. You can't know at config time.

2. **Cognitive overhead**: New users must understand the stage pipeline and model
   tradeoffs before they can write a useful `forge.yaml`. "I have access to
   Claude, Codex, and Gemini — just use them well" is the natural starting point.

This is the sprint planning estimation problem: you don't know how many points
a task is until you start working on it. The preflight agent already reads the
codebase and the spec — it should emit a complexity signal that drives model
assignment.

## Design

### Declarative model pool

A new top-level `models` section declares available models without assigning
them to stages:

```yaml
models:
  - name: opus
    cli: claude
    model: opus
  - name: sonnet
    cli: claude
    model: sonnet
  - name: codex
    cli: codex
    model: gpt-5.4
    reasoning_effort: medium
  - name: gemini
    cli: gemini
    model: gemini-2.5-pro

strategy: auto          # "auto" (default) or "manual"
budget_usd: 80.00       # total sprint/run budget
```

When `strategy: auto`, forge assigns models to stages. When `strategy: manual`
(or when `profiles:` is present), the current explicit config is used unchanged.
Full backward compatibility — existing `forge.yaml` files work exactly as before.

### Complexity signal from preflight

The preflight prompt already instructs the model to assess the task. We extend
the expected output to include a complexity assessment:

```
complexity: small | medium | large
```

The coordinator parses this from the preflight output (best-effort — if missing,
default to `medium`). This is NOT a new LLM decision about process — it's a
data point the preflight already implicitly determines. The coordinator's
response to the signal is still deterministic Python.

### Stage assignment rules

The coordinator uses a pure-Python function `_assign_models()` that takes the
model pool, complexity signal, and budget to produce a `StageAssignment`:

```python
@dataclass(frozen=True)
class StageAssignment:
    """Deterministic model-to-stage mapping produced by _assign_models()."""
    dev_profile: ModelProfile
    preflight_profile: ModelProfile
    review_pool: list[ModelProfile]
    synthesis_profile: ModelProfile | None
```

#### Assignment heuristics (deterministic, no LLM):

**Ranking**: Models are ranked by capability tier:
- Tier 1 (strongest): opus
- Tier 2 (balanced): sonnet, gpt-5.4, gemini-2.5-pro
- Tier 3 (fast): haiku, gpt-4.1-mini

The tier is derived from the model name string using a static lookup table.
Unknown models default to Tier 2.

**Preflight**: Always uses the cheapest available model (Tier 3 > Tier 2 > Tier 1).
Preflight is read-only gap analysis — doesn't need a strong model.

**Dev assignment by complexity**:

| Complexity | Dev model | Rationale |
|------------|-----------|-----------|
| small | Cheapest Tier 2 | Fast iteration, low cost |
| medium | Best Tier 2 | Good balance |
| large | Best Tier 1 (or best Tier 2 if no Tier 1) | Hard problems need strong models |

**Review pool by pool size**:

| Available models | Review pool | Synthesis |
|-----------------|-------------|-----------|
| 1 model | That model reviews its own work (same as today) | None |
| 2 models | The non-dev model reviews | None |
| 3+ models | All models except dev model | Best available (prefer Tier 1) |

The dev model is excluded from the review pool to ensure independent review.
If only one model is available, it serves as both dev and reviewer (current
behavior for single-model configs).

**Budget allocation**: Each profile gets a budget slice:
- Dev: 60% of remaining budget
- Review pool: 30% split evenly across reviewers
- Synthesis: 5%
- Preflight: 5%

These are starting allocations. The existing budget enforcement (cumulative cost
tracking) is the real safety valve — these just set per-invocation caps.

---

## Requirements

### R1: `models` section in forge.yaml

New optional top-level key. Each entry has:
- `name` (required): unique identifier
- `cli` (required): "claude", "codex", or "gemini"
- `model` (required): model identifier string
- `reasoning_effort` (optional): for Codex
- `budget_usd` (optional): per-model override; if omitted, auto-allocated

Validation:
- `cli` must be in `SUPPORTED_CLIS`
- `name` must be unique
- At least 1 model required when `models` is present

### R2: `strategy` key in forge.yaml

- `strategy: auto` (default when `models` is present): forge assigns models
- `strategy: manual` (default when `profiles` is present): current behavior

If both `models` and `profiles` are present, raise `ValueError` — pick one.
If neither is present, use defaults (current behavior).

### R3: Complexity signal from preflight

Add to the preflight prompt suffix:

```
Also assess the implementation complexity of this task:
complexity: small | medium | large

small = config change, typo fix, simple addition (<50 lines changed)
medium = new feature, moderate scope (50-300 lines)
large = cross-cutting refactor, many files, architectural change (300+ lines)
```

Parse from preflight output using regex: `complexity:\s*(small|medium|large)`.
Default to `medium` if not found. Store on `CoordinatorResult` for audit trail.

### R4: `_assign_models()` in config.py

```python
MODEL_TIERS: dict[str, int] = {
    "opus": 1,
    "claude-opus-4": 1,
    "sonnet": 2,
    "claude-sonnet-4": 2,
    "gpt-5.4": 2,
    "o4-mini": 2,
    "gemini-2.5-pro": 2,
    "haiku": 3,
    "claude-haiku-3.5": 3,
    "gpt-4.1-mini": 3,
    "gemini-2.0-flash": 3,
}

def _get_tier(model: str) -> int:
    """Return capability tier for a model. Unknown models default to Tier 2."""
    return MODEL_TIERS.get(model, 2)

def assign_models(
    pool: list[ModelProfile],
    complexity: str,
    total_budget: float,
) -> StageAssignment:
    """Deterministic model-to-stage assignment. No LLM involved."""
    ...
```

This is a pure function. Easy to test, easy to reason about, no LLM in the loop.

### R5: Config loader integration

`load_config()` changes:
- If `models` key exists (and no `profiles`), parse the model pool and store it
  on `ForgeConfig` as `model_pool: list[ModelProfile] | None`
- Add `strategy: str` field to `ForgeConfig` (default: `"manual"`)
- Add `total_budget_usd: float | None` to `ForgeConfig`
- The coordinator calls `assign_models()` after preflight returns the complexity
  signal, then uses the resulting `StageAssignment` for the rest of the run

When `strategy == "manual"`, behavior is identical to today. The `model_pool`
field is `None` and `dev_profile`/`review_pool` come from `profiles` as before.

### R6: Coordinator integration

In `_run()`, after PREFLIGHT:

```python
if self.config.strategy == "auto":
    complexity = self._parse_complexity(preflight_output)
    assignment = assign_models(
        self.config.model_pool,
        complexity,
        self.config.total_budget_usd,
    )
    # Override profiles for this run
    self._dev_profile = assignment.dev_profile
    self._review_pool = assignment.review_pool
    self._synthesis_profile = assignment.synthesis_profile
```

Log the assignment:

```
[forge]   Complexity: medium → dev=sonnet, review=[opus, codex, gemini], synthesis=opus
```

### R7: CLI display

Update the header block to show pool-based config:

```
TheForge v0.1.0
  Project:    hdp
  Strategy:   auto (3 models)
  Pool:       opus, sonnet, codex
  Budget:     $80.00
```

vs. the current explicit display when `strategy: manual`.

### R8: Audit trail

`CoordinatorResult` gains:
- `complexity: str | None` — the preflight complexity signal
- `stage_assignment: dict | None` — the auto-assigned model mapping

Both written to the audit log for analysis. Over time, `history.jsonl` will
show whether the auto-assignment heuristics are making good choices.

### R9: Tests

- `test_assign_models_small`: 3-model pool + small → cheapest Tier 2 for dev,
  others review
- `test_assign_models_medium`: 3-model pool + medium → best Tier 2 for dev
- `test_assign_models_large`: 3-model pool + large → Tier 1 for dev
- `test_assign_models_single`: 1-model pool → same model for dev and review
- `test_assign_models_two`: 2-model pool → one dev, one review, no synthesis
- `test_assign_models_budget_split`: verify budget percentages
- `test_parse_complexity_from_preflight`: regex extraction
- `test_parse_complexity_missing_defaults_medium`: no signal → medium
- `test_models_and_profiles_both_present_raises`: mutual exclusion
- `test_strategy_manual_ignores_pool`: explicit profiles used as-is
- `test_backward_compat_no_models_section`: existing configs work unchanged

---

## Acceptance Criteria

1. `models:` section in `forge.yaml` declares available models without stage assignment
2. `strategy: auto` causes forge to assign models to stages after preflight
3. Preflight emits a complexity signal (small/medium/large)
4. `_assign_models()` is a pure deterministic function — no LLM, no network
5. Existing `forge.yaml` files with `profiles:` work unchanged (full backward compat)
6. Having both `models:` and `profiles:` raises a clear error
7. Audit log includes complexity signal and auto-assignment for analysis
8. CLI header displays pool-based config when using `strategy: auto`
9. All existing tests pass
10. New tests cover assignment heuristics and config parsing

## Out of Scope

- **Runtime model escalation** (Level 3): swapping dev model mid-run if iterations
  aren't converging. The audit history will tell us if/when this is needed.
- **Cost-optimal assignment**: choosing models based on $/token efficiency. Budget
  enforcement already handles overspend; assignment is about capability matching.
- **Model capability probing**: testing model availability before assignment.
  If a model fails, the existing error handling surfaces it.
- **Per-spec strategy overrides**: all specs in a sprint use the same strategy.
  Could add spec-level `complexity: large` hints later if needed.
