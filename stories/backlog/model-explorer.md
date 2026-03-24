---
name: "Model explorer — empirical routing via challenger sampling"
slug: model-explorer
pytest_target: tests/
depends_on: [domain-aware-routing]
---

# Model Explorer: Challenger Sampling for Adaptive Routing

## Problem

The adaptive router exploits what it knows but never explores. It escapes bad
choices (escalation memory) but has no mechanism to *discover* that a different
model might be better. The performance table in the vision doc is empty — telemetry
is written to YAML and never read back by the router.

A system that always picks the same model for a given profile will never learn
that Gemini Flash is fast at preflight, or that DeepSeek is cost-effective for
simple dev tasks. It can only learn from failures, not from trying alternatives.

## The Mongo Query Planner Analogy

MongoDB doesn't pick an index based on theory. It races competing query plans
on sampled queries and caches the winner. When collection stats drift, it re-races.

Theforge should do the same: for every Nth story matching a given profile
(phase + domain + complexity band), run a *challenger* model instead of the
cached winner and record the outcome. The router learns from the race.

## Solution

### Performance table

Persist a `performance_table.yaml` under `.forge/` that tracks observed outcomes
per routing key:

```
key: (phase, domain, complexity_band)     # e.g. dev/backend-api/medium
model: claude-sonnet-4-5
runs: 12
success_rate: 0.83
avg_cost_usd: 1.40
avg_iterations: 2.1
avg_duration_s: 340
last_updated: 2026-03-23
```

After every story run, append/update the record for its routing key.

### Challenger sampling

Every Nth run for a given routing key (configurable, default N=5), the router
picks a *challenger* instead of the current best:

- Challenger is selected randomly from available agents not currently winning
  that slot
- Outcome is recorded the same way
- If the challenger outperforms the winner across success rate and cost,
  it becomes the new winner

### Cold start

When no data exists for a routing key, use the existing tier table as the default
and mark the slot as "exploring." All runs in exploring state are treated as
challenger races until a minimum sample size (default: 3) is reached.

### Config

```yaml
adaptive:
  explore_every_n: 5        # challenger run frequency per routing key
  min_sample_size: 3        # runs before a winner is declared
  performance_db: .forge/performance_table.yaml
```

## Acceptance criteria

- Performance table written after every story run
- Challenger sampling fires every N runs per routing key
- Winner updated when challenger outperforms on success rate (cost as tiebreaker)
- Cold start routes via tier table, records to performance table
- Exploration visible in telemetry (`model_selection: "challenger"` vs `"winner"`)
- All existing tests pass
- New tests for table update, challenger selection, winner promotion
