---
name: "Smart defaults — opinionated starting config and forge init scaffolding"
slug: smart-defaults
pytest_target: tests/
---

# Smart Defaults

## Problem

New users hit a blank `forge.yaml` and have to figure out model tiers,
pool composition, retry limits, tool permissions, budgets, and timeouts
from scratch. Even experienced users cargo-cult bad patterns (Opus on dev,
Haiku on review) because there's no guidance baked into the system.

The accumulated evidence from SWE-bench, Aider benchmarks, and production
multi-agent systems points to clear best-practice defaults. These should
be the starting point, not a wiki page.

## Solution

1. **`forge init`** scaffolds an opinionated `forge.yaml` with documented
   defaults and inline comments explaining the "why" behind each choice.
2. **Built-in defaults** in `config.py` fill in missing values so a minimal
   `forge.yaml` (just `project:` and one dev profile) produces a working
   run with sane behavior.
3. **Validation warnings** when config deviates from known-good patterns
   (e.g., review model weaker than dev model, iteration limits > 5).

## Default Model Tiering

The core principle: **spend on leverage, not on volume.**

| Phase        | Default tier | Rationale                                          |
|-------------|-------------|-----------------------------------------------------|
| Preflight   | Cheap/fast   | Triage gate — reading specs + files, binary output  |
| Plan        | Strong       | Highest leverage — shapes entire dev cycle          |
| Plan Review | Strong, different provider | Same-model blind spots are real     |
| Dev         | Mid-tier     | Most tokens here — cost efficiency matters          |
| Code Review | Strong, multi-provider pool | Last gate — must catch what dev missed |

## Default forge.yaml (scaffolded by `forge init`)

```yaml
project: my-project

workspace:
  root: .forge/worktrees
  branch_prefix: feat/
  on_approve: none          # none | pr | merge

profiles:
  preflight:
    provider: anthropic
    model: haiku
    budget_usd: 0.50
    timeout_seconds: 120

  planner:
    provider: anthropic
    model: opus
    budget_usd: 3.00
    timeout_seconds: 300

  dev:
    provider: anthropic
    model: sonnet
    budget_usd: 5.00
    timeout_seconds: 900
    max_iterations: 50
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]

  review-claude:
    provider: anthropic
    model: opus
    budget_usd: 3.00
    timeout_seconds: 480
    allowed_tools: [Read, Glob, Grep]

  review-deepseek:
    provider: deepseek
    model: deepseek-reasoner
    budget_usd: 1.00
    timeout_seconds: 480
    allowed_tools: [Read, Glob, Grep]

review:
  pool: [review-claude, review-deepseek]

plan_agent_review:
  enabled: true
  pool: [review-claude, review-deepseek]

retry:
  max_dev_iterations: 3
  max_review_cycles: 2
  max_plan_regen_attempts: 2

gate:
  command: make gate
```

## Built-in Defaults (when key is omitted)

Applied by `config.py` when parsing forge.yaml:

```python
# Retry policy
max_dev_iterations = 3        # not 5 or 10 — if stuck, escalate
max_review_cycles = 2         # first cycle catches, second verifies
max_plan_regen_attempts = 2   # two bad plans = spec needs human

# Timeouts (seconds)
preflight_timeout = 120
plan_timeout = 300
dev_timeout = 900
review_timeout = 480

# Budgets (USD)
preflight_budget = 0.50
plan_budget = 3.00
dev_budget = 5.00
review_budget = 3.00

# Nudges
iteration_nudge_pct = 0.80    # fire at 80% of max_iterations
time_nudge_pct = 0.80         # fire at 80% of timeout
```

## `forge init` Command

```bash
forge init                    # scaffold forge.yaml with defaults + comments
forge init --minimal          # project + single dev profile only
forge init --providers anthropic,deepseek  # pre-fill provider-specific profiles
```

The scaffolded file includes inline comments explaining each choice:

```yaml
# Dev: mid-tier model — most tokens spent here, cost matters more than
# peak intelligence. Plan quality drives success more than dev model strength.
dev:
  provider: anthropic
  model: sonnet
```

## Validation Warnings

At config parse time, emit warnings (not errors) for:

- Review model weaker than dev model (e.g., Haiku review, Opus dev)
- Single-provider review pool (correlated blind spots)
- max_dev_iterations > 5 (diminishing returns, likely burning budget)
- max_review_cycles > 3 (same)
- No plan review enabled (high-leverage phase unguarded)
- Review profiles with write tools (reviewers shouldn't edit)
- Dev profile without Bash tool (can't run tests)

## Acceptance Criteria

- `forge init` creates a commented forge.yaml with the default tiering
- `forge init --minimal` creates a minimal working config
- `forge init --providers` pre-fills profiles for specified providers
- `forge init` refuses to overwrite existing forge.yaml (--force to override)
- Config parsing fills missing retry/timeout/budget values from built-in defaults
- Validation warnings fire for known anti-patterns (logged, not blocking)
- Existing explicit configs are never overridden by defaults
- All defaults documented in `docs/guides/inputs-reference.md`
- Tests cover default filling, validation warnings, and init scaffolding
