# Epic: Config Simplification — Two-Tier forge.yaml Redesign

## Vision

forge.yaml has four overlapping model-selection surfaces (`profiles`,
`smart_config_models`, `agents`, `assignment`) that grew organically. Users must
understand a priority cascade to write a basic config. Replace with a two-tier
schema: `models` for the happy path, `overrides` for expert tweaks.

Parent epic: intelligent-defaults (this is the schema capstone).
Absorbs: config-normalization (same loader change).

## Target state

**Tier 1 — simple mode (5 lines):**
```yaml
project: myproject
models: [claude/sonnet, openai/gpt-5.4, claude/opus]
budget_usd: 50
gate: "pytest tests/ -q"
on_approve: merge-pr
```

**Tier 2 — expert overrides (only when diverging from derived defaults):**
```yaml
overrides:
  dev: {model: opus, timeout_large: 1800}
  plan: {model: opus}
  review_pool:
    - {name: deepseek-reviewer, provider: deepseek, model: deepseek-reasoner, role: patterns}
```

## Stories (ordered by dependency)

1. **config-schema-definition** — define the new schema types, validation rules,
   and derivation logic. No loader changes yet — just the target types and a
   standalone `derive_roles(models, overrides) -> ForgeConfig` function with tests.

2. **config-loader-v2** — implement the new loader in `config/load.py` that
   parses the new schema and rejects old keys with clear migration errors.
   Wire it into the coordinator. `ForgeConfig` internals stay compatible.

3. **config-dogfood-and-tests** — rewrite theforge's forge.yaml to the new
   schema. Update all test fixtures and config factories to use new schema.
   Verify `forge check-config` displays derived assignments.

4. **config-dead-code-removal** — delete old config paths: `smart_config_models`
   derivation, `profiles` parsing, `agents` parsing, `model_name` compat shim.
   Remove dead types from `config/types.py`.

## Key design constraints

- `overrides` must stay surgical — don't let it become `profiles` under a new name
- `models` is the one true model-selection field
- Operational config (`workspace`, `validation`, `conventions`, `hooks`,
  `notifications`) stays structured — don't flatten everything
- Old keys fail hard with crisp before/after migration errors
- Internal `ForgeConfig` / `ModelProfile` objects can stay as-is — this is a
  parsing/loading change, not a runtime change
