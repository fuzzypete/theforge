# Epic: Intelligent Defaults — From Blank Config to Self-Tuning System

## Vision

A new user runs `forge init`, gets a config that works well out of the box,
and the system gets smarter over time by tracking what works and what doesn't.
No manual model-shopping, no guessing at iteration limits, no surprise $50 runs.

The progression:
1. **Smart defaults** — opinionated starting config based on ecosystem evidence
2. **Health metrics** — visibility into whether the config is working well
3. **Adaptive assignment** — system picks models per story based on complexity
4. **Escalation learning** — system promotes models when current tier fails too often

## Stories (ordered)

### Phase 1: Foundation
- [ ] `smart-defaults` — `forge init` scaffolding, built-in defaults in config.py,
      validation warnings for anti-patterns
- [ ] `run-health-metrics` — per-phase cost/timing tracking, anomaly detection,
      health summary in run output and audit YAML

### Phase 2: Adaptive
- [ ] `adaptive-model-assignment` — complexity-driven model routing, cross-provider
      review pools, budget caps (depends on smart-defaults + api-mode-dev)

### Phase 3: Learning
- [ ] `escalation-learning` — track escalation history per complexity tier,
      auto-promote models after repeated failures (can be split from adaptive
      or built as part of it)

## Dependencies

```
smart-defaults
  ├── run-health-metrics (uses default reference ranges)
  └── adaptive-model-assignment (uses tier definitions)
        └── escalation-learning (uses assignment + health data)

api-mode-dev (already landed)
  └── adaptive-model-assignment (needs API agents in the pool)
```

## Key Design Constraints

- **All assignment logic is deterministic** — no LLM in the routing loop
- **Explicit config always wins** — adaptive never overrides what the user set
- **Health metrics are mechanical** — pure math on data the coordinator already collects
- **Escalation learning is local** — stored in `.forge/`, not shipped anywhere
- **Reference ranges are configurable** — but the defaults should be right for 90% of users

## Definition of Done

- `forge init` produces a working config that a new user can run immediately
- Health metrics surface in every run summary (verbose) and audit trail
- Adaptive assignment makes per-story model decisions from the available pool
- Escalation history auto-promotes models after repeated failures for a tier
- A sprint of 5 MEDIUM stories with adaptive enabled costs < $60 total
- First-pass success rate with smart defaults ≥ 70% on MEDIUM stories
