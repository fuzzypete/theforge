---
name: "Cache-aware cost estimation and provider reconciliation"
slug: cache-aware-cost-estimation
pytest_target: tests/
---

# Cache-Aware Cost Estimation

## Problem

theforge records cache_read_tokens and cache_creation_tokens from Anthropic
and Google adapters, but the cost estimator only prices input and output
tokens. All prompt tokens — including those served from cache at reduced
rates — are charged at full input price. This consistently overestimates
costs, sometimes by 10-15%.

There's also no way to compare local estimates against provider actuals
to detect pricing drift, missing token categories, or estimation bugs.

## Acceptance Criteria

### Phase 1: Cache-aware estimation

- [ ] Pricing table uses 4 rates per model: input, output, cache_read,
      cache_create (None when rate is unknown)
- [ ] Cost estimator accepts ModelUsage (not just input/output token counts)
- [ ] Uncached input computed explicitly:
      `max(0, input_tokens - cache_read_tokens - cache_creation_tokens)`
- [ ] When cache rates are known, all 4 categories priced separately
- [ ] When cache rates are unknown, only uncached input + output priced
      (never double-charges cached tokens as full-price input)
- [ ] Anthropic and Google runs use cache-aware pricing
- [ ] OpenAI and DeepSeek behavior unchanged (no cache semantics yet)
- [ ] Each cost estimate tagged with estimate_mode:
      simple_io | uncached_input_only | cache_aware_full | unknown_pricing
- [ ] Audit output preserves both simple and cache-aware estimates for
      comparison during calibration

### Phase 2: Config-driven pricing overrides

- [ ] forge.yaml supports optional `pricing_overrides` block per
      provider/model with input, output, cache_read, cache_create rates
- [ ] Override takes precedence over built-in table
- [ ] Missing cache rates in override → estimator falls back to
      uncached_input_only mode
- [ ] Pricing tunable without code changes

### Phase 3: Reconciliation script

- [ ] Standalone script (scripts/usage_reconcile.py or similar) that
      aggregates local estimates from audit/log files by date/provider/model
- [ ] Produces discrepancy report comparing local estimates against
      provider actuals (entered manually or fetched where API allows)
- [ ] Report shows: provider, model, window, local estimate, actual,
      delta, delta %, likely cause
- [ ] Thresholds documented: <5% acceptable, 5-15% tune overrides,
      >15% investigate

### General

- [ ] All existing tests pass
- [ ] New tests for 4-rate pricing, uncached input calculation,
      estimate_mode tagging, and config override loading
