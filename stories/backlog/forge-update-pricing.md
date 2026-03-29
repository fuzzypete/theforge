---
name: "forge update-pricing — fetch and cache current provider pricing"
slug: forge-update-pricing
pytest_target: tests/
---

# forge update-pricing

## Problem

The internal pricing table is hardcoded at release time. Provider pricing
changes frequently — new models are added, preview models graduate to GA
with different rates, existing prices are adjusted. When the table is
stale, cost estimates are wrong and newly added models show `cost: null`
in telemetry.

The immediate trigger: `gemini-3.1-pro-preview-customtools` was added to
`forge.yaml` but is absent from the pricing table, so every run with that
reviewer reports unknown cost.

Manually updating the table requires a code change and a release. There
is no operator-facing way to refresh pricing without touching source.

## Solution

A `forge update-pricing` command that fetches current pricing from
provider documentation, writes a local pricing cache, and reports what
changed. The cache is used to supplement (not replace) the built-in
table — built-in entries are overridden by cache entries, unknown models
resolve from the cache.

```
$ forge update-pricing

Fetching pricing from providers...
  ✓ Anthropic    (12 models)
  ✓ OpenAI       (18 models)
  ✓ Google       (14 models)
  ✓ DeepSeek     (4 models)

Cache written: .forge/pricing-cache.yaml
Last updated:  2026-03-29T14:00:00Z

Changes from built-in table:
  + google / gemini-3.1-pro-preview-customtools   $3.50 / $10.50
  + google / gemini-3.1-flash-preview             $0.15 / $0.60
  ~ google / gemini-2.5-pro  input $1.25 → $2.50  (built-in was stale)
```

The cache file is human-readable YAML so operators can inspect or
manually correct entries without a code change.

## Requirements

1. `forge update-pricing` fetches pricing for all providers configured
   in `forge.yaml` — only fetches what is actually in use
2. Results are written to `.forge/pricing-cache.yaml` with a timestamp
3. The cache supplements the built-in table: cache entries take
   precedence, built-in entries fill gaps not covered by the cache
4. On each run, forge loads the cache if present and merges it silently
5. `forge update-pricing --dry-run` prints what would change without
   writing the cache
6. `forge update-pricing --provider google` fetches a single provider
7. The command reports new models, changed prices, and unchanged models
   separately so the operator can see what drifted
8. Cache is not required for forge to run — absent cache falls back to
   built-in table with no error

## Acceptance Criteria

- `forge update-pricing` writes `.forge/pricing-cache.yaml` with fetched
  pricing data and a `fetched_at` timestamp
- Cache is loaded at runtime and takes precedence over built-in table
  entries for matching models
- Models in the cache but not the built-in table resolve correctly in
  cost estimation and telemetry (no `null` cost)
- `--dry-run` prints changes without writing the file
- `--provider <name>` fetches only that provider
- Output clearly distinguishes new models (+), changed prices (~), and
  confirmed-unchanged models (=)
- If a provider's pricing page cannot be fetched, that provider is
  skipped with a warning and existing cache entries for it are preserved
- All existing tests pass
- New tests cover: cache load and merge, cache-wins-over-builtin,
  missing cache falls back gracefully, dry-run produces no file

## Out of Scope

- Automatic pricing refresh on a schedule
- Pricing for CLI-mode profiles (claude, codex) — API cost tracking only
- Price history or trend tracking
- Alerting on price changes
