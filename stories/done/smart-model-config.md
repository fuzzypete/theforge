---
name: "smart model config: declarative pool + complexity-adaptive assignment"
slug: smart-model-config
file_scope:
  - src/theforge/config.py
  - src/theforge/coordinator.py
  - src/theforge/task.py
  - tests/test_config.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Smart Model Config

## Problem

Today's `forge.yaml` requires users to manually wire models to stages:

```yaml
profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 50.00
    ...
  review_pool:
    - name: opus
      cli: claude
      model: opus
      ...
    - name: codex
      cli: codex
      model: gpt-5.4
      ...
  synthesis:
    cli: claude
    model: opus
    ...
```

This has three problems:

1. **Boilerplate**: HDP and TheForge configs are 80+ lines of near-identical
   profile blocks. Every new project copies the same template.
2. **Premature commitment**: You choose models before knowing task complexity.
   A config tweak doesn't need opus review; a cross-cutting refactor does.
3. **No complexity awareness**: Every spec gets the same model allocation
   regardless of whether preflight signals "trivial rename" or "major
   architectural change."

## Design

### Level 1: Declarative model pool

A new top-level `models` key declares available models. Forge assigns them
to stages automatically using heuristics.

```yaml
# Minimal smart config
project: hdp
models:
  - claude/sonnet
  - claude/opus
  - openai/gpt-5.4
  - google/gemini-2.5-pro
budget_usd: 50.00
```

That's it. Forge resolves this to:

| Stage | Assignment | Rationale |
|-------|-----------|-----------|
| preflight | cheapest (sonnet) | Read-only gap analysis, speed matters |
| dev | cheapest capable (sonnet) | Iterates fast, budget is safety valve |
| review_pool | all remaining models | Maximum diversity for review |
| synthesis | strongest (opus) | Needs to reconcile conflicting reviews |

#### Model registry

Forge maintains a built-in registry of known models with metadata:

```python
MODEL_REGISTRY: dict[str, ModelInfo] = {
    "claude/sonnet": ModelInfo(
        cli="claude", model="sonnet",
        tier="fast", capability=7, cost_rank=1,
    ),
    "claude/opus": ModelInfo(
        cli="claude", model="opus",
        tier="strong", capability=10, cost_rank=3,
    ),
    "openai/gpt-5.4": ModelInfo(
        cli="codex", model="gpt-5.4",
        tier="strong", capability=9, cost_rank=2,
    ),
    "google/gemini-2.5-pro": ModelInfo(
        cli="gemini", model="gemini-2.5-pro",
        tier="strong", capability=8, cost_rank=2,
    ),
}
```

- `tier`: "fast" or "strong" — controls stage eligibility
- `capability`: relative ranking (higher = more capable) — breaks ties
- `cost_rank`: 1=cheap, 2=moderate, 3=expensive — drives dev assignment

#### Assignment algorithm

```
1. Sort models by cost_rank ascending, capability descending
2. dev = first model (cheapest capable)
3. preflight = first model with tier "fast", else same as dev
4. review_pool = all models except dev (if only 1 model, review_pool = [dev])
5. synthesis = model with highest capability from review_pool
   (skip if pool size <= 1)
```

When only 1 model is declared, it's used for everything (single-model mode,
already supported).

When 2 models declared: cheaper one → dev+preflight, other → single reviewer
(no synthesis needed).

#### Budget distribution

The top-level `budget_usd` is distributed across profiles:

| Profile | Share |
|---------|-------|
| dev | 60% of budget |
| preflight | 2% of budget (min $1) |
| each reviewer | remaining / pool_size |
| synthesis | 2% of budget (min $1) |

Users can override any individual budget with explicit profile config.

### Level 2: Complexity-adaptive assignment

Preflight gains a `complexity` output field alongside `verdict`:

```
VERDICT: PROCEED
COMPLEXITY: medium
REASON: New feature touching 3 modules with test coverage requirements.
```

The coordinator reads complexity and adjusts model assignment:

| Complexity | Dev model | Review pool | Synthesis |
|------------|-----------|-------------|-----------|
| small | cheapest (sonnet) | single cheapest reviewer | skip |
| medium | cheapest (sonnet) | full pool | yes (if pool > 1) |
| large | strongest available (opus) | full pool | yes (always) |

#### Complexity heuristics in preflight prompt

The preflight prompt already asks "should the dev agent proceed?" — we extend
it to also assess complexity:

```
Assess the complexity of this task:
- SMALL: config change, typo fix, single-file edit, <50 lines changed
- MEDIUM: new feature, multi-file change, requires tests, 50-500 lines
- LARGE: cross-cutting refactor, architectural change, >500 lines, many modules

Output a COMPLEXITY line: small, medium, or large.
```

The coordinator parses this from preflight output alongside the existing
verdict parsing.

---

## Requirements

### R1: Model registry

New `ModelInfo` dataclass and `MODEL_REGISTRY` dict in `config.py`.

```python
@dataclass(frozen=True)
class ModelInfo:
    """Built-in metadata for a known model."""
    cli: str           # "claude", "codex", "gemini"
    model: str         # model identifier for the CLI
    tier: str          # "fast" or "strong"
    capability: int    # relative capability score (1-10)
    cost_rank: int     # 1=cheap, 2=moderate, 3=expensive
```

Initial registry covers the 4 models currently supported. Unknown models
(not in registry) default to tier="strong", capability=5, cost_rank=2.

### R2: `models` key in forge.yaml

New optional top-level key. Format: `"provider/model"` strings.

```yaml
models:
  - claude/sonnet
  - claude/opus
  - openai/gpt-5.4
  - google/gemini-2.5-pro
```

Each string is looked up in `MODEL_REGISTRY`. Unknown models are accepted
with defaults (so the registry doesn't gate adoption of new models).

### R3: `budget_usd` top-level key

When `models` key is present, a top-level `budget_usd` sets the overall
budget. Distributed across profiles per the share table in Design.

### R4: Auto-assignment function

```python
def _auto_assign_models(
    models: list[str],
    budget_usd: float,
    overrides: dict[str, Any] | None = None,
) -> tuple[ModelProfile, ModelProfile, list[ModelProfile], ModelProfile | None]:
    """Returns (dev, preflight, review_pool, synthesis) profiles."""
```

Implements the assignment algorithm from Design. Returns typed `ModelProfile`
objects ready for `ForgeConfig`.

### R5: `load_config` integration

When `models` key is present in forge.yaml:
1. Call `_auto_assign_models()` to generate profiles
2. Any explicit `profiles` section entries override the auto-assigned ones
   (partial override — user can set `profiles.dev.budget_usd: 100` without
   specifying every field)
3. `workspace`, `validation`, `retry`, `notifications` sections work
   unchanged

When `models` key is absent, behavior is 100% backward compatible —
existing `profiles` section is used exactly as today.

### R6: Complexity output from preflight

Extend the preflight prompt (in `task.py`) to request complexity assessment.

Parse `COMPLEXITY: small|medium|large` from preflight output in coordinator.
Default to `medium` if not found (safe middle ground).

Store complexity on `CoordinatorResult` or as a local variable in the
coordinator loop.

### R7: Complexity-adaptive model swapping

When smart config is active (`models` key present) AND complexity is parsed:

- `small`: downgrade review_pool to single cheapest reviewer, skip synthesis
- `medium`: use auto-assigned defaults (no change)
- `large`: upgrade dev to strongest available model

When explicit `profiles` are used (no `models` key), complexity is logged
but does NOT override the user's explicit model choices.

### R8: CLI display

Show the auto-assigned configuration in the run header:

```
[forge]   Models: claude/sonnet, claude/opus, openai/gpt-5.4, google/gemini-2.5-pro
[forge]   Auto-config: dev=sonnet, review=[opus, gpt-5.4, gemini-2.5-pro], synthesis=opus
[forge]   Complexity: medium (from preflight)
```

### R9: Backward compatibility

- No `models` key → existing behavior, zero changes
- `models` + `profiles` → auto-assign first, then overlay explicit profiles
- Existing forge.yaml files work without modification

### R10: Config validation

- `models` list must be non-empty if present
- Each entry must be `"provider/model"` format (contains `/`)
- Provider must map to a supported CLI (claude→claude, openai→codex,
  google→gemini), or the model must be in the registry
- Budget must be positive

### R11: Tests

- `test_auto_assign_4_models`: 4 models → correct dev/preflight/pool/synthesis
- `test_auto_assign_2_models`: 2 models → dev + single reviewer, no synthesis
- `test_auto_assign_1_model`: 1 model → used for everything
- `test_auto_assign_budget_distribution`: verify budget shares
- `test_models_key_loads_config`: forge.yaml with `models` key produces valid ForgeConfig
- `test_models_with_profile_override`: explicit profiles overlay auto-assigned
- `test_models_key_absent_backward_compat`: no `models` key → existing behavior
- `test_unknown_model_gets_defaults`: model not in registry → accepted with defaults
- `test_complexity_parsed_from_preflight`: small/medium/large parsed correctly
- `test_complexity_default_medium`: missing complexity line → medium
- `test_complexity_small_reduces_review_pool`: small → single reviewer
- `test_complexity_large_upgrades_dev`: large → strongest model for dev
- `test_complexity_ignored_with_explicit_profiles`: no `models` key → complexity logged only

---

## Acceptance Criteria

1. `models` key in forge.yaml auto-assigns models to all stages
2. Preflight emits complexity signal (small/medium/large)
3. Complexity adjusts model allocation when smart config is active
4. Existing forge.yaml files without `models` key work identically
5. Explicit `profiles` entries override auto-assigned values
6. All existing tests pass unchanged
7. New tests cover auto-assignment, budget distribution, complexity parsing,
   and adaptive model swapping

## Out of Scope

- Level 3 (runtime adaptation / mid-run model escalation) — future spec
- Model capability benchmarking (registry values are hand-tuned)
- Cost estimation before run (would need API pricing data)
- Auto-discovery of available models (user declares what they have)

## Migration

Users can adopt incrementally:

```yaml
# Before (still works)
profiles:
  dev: ...
  review_pool: ...

# After (new minimal config)
models:
  - claude/sonnet
  - claude/opus
budget_usd: 50.00

# Hybrid (auto-assign + override dev budget)
models:
  - claude/sonnet
  - claude/opus
budget_usd: 50.00
profiles:
  dev:
    budget_usd: 100.00
```
