# Epic: Intelligent Defaults → Self-Tuning System

## Vision

A new user runs `forge init`, gets a config that works well out of the box,
and the system gets smarter over time. The number of YAML knobs shrinks as
forge learns to derive limits from its own run data. The end state: one
budget knob per story, everything else derived.

The progression:
1. **Smart defaults** — opinionated starting config based on ecosystem evidence
2. **Domain-aware routing** — preflight classifies domain + complexity (1-10),
   router matches agent strengths *(in flight)*
3. **Adaptive resource budgets** — iteration/timeout limits derived from
   complexity score and learned agent behavior profiles, replacing manual
   per-agent knobs
4. **Progress-aware timeouts** — stuck detection within the adaptive ceiling
5. **Escalation learning** — auto-promote models when current tier fails
   too often for a given complexity band

## Stories (ordered by dependency)

### Phase 1: Foundation *(shipped)*
- [x] `smart-model-config` — model tiering in forge.yaml
- [x] `adaptive-model-assignment` — complexity-driven model routing
- [x] `dev-model-escalation` — persistent P1 triggers model promotion
- [x] `phase-telemetry` — per-phase cost/timing tracking

### Phase 1a: Dogfood hardening *(next)*
- [ ] `adaptive-assignment-runtime-integration` — prove coordinator runtime
      wiring, override preservation, and history writes end to end
- [ ] `adaptive-assignment-audit-trail` — expose assignment choices,
      overrides, rationale, and promotion in the audit log
- [ ] `assignment-history-end-to-end` — prove the escalation-memory learning
      loop with a small fixture

### Phase 2: Domain + config (v0.2.1) *(in flight)*
- [ ] `config-normalization` — unify model spec, loud validation on load
- [ ] `domain-aware-routing` — 1-10 complexity, domain tags, strengths matching
- [ ] `forge-check-config` — show effective config, auth checks, exit codes

### Phase 3: Adaptive limits
- [ ] `complexity-resource-scaling` — learned agent profiles replace manual
      per-agent max_iterations/timeout/budget knobs
- [ ] `progress-aware-timeouts` — stuck detection via tool-call patterns,
      nudge then terminate

### Phase 4: Learning loop
- [ ] `escalation-learning` — read history, detect repeat failures at a tier,
      auto-promote before story starts

## Dependencies

```
config-normalization
  └── forge-check-config

domain-aware-routing
  └── complexity-resource-scaling
        └── progress-aware-timeouts (composes: ceiling + early exit)

adaptive-model-assignment (shipped)
  ├── adaptive-assignment-runtime-integration
  ├── adaptive-assignment-audit-trail
  └── assignment-history-end-to-end
        └── escalation-learning
```

## Key Design Constraints

- **All assignment/scaling logic is deterministic** — no LLM in the routing loop
- **Explicit config always wins** — adaptive never overrides what the user set
- **Learned profiles are local** — stored in `.forge/`, not shipped anywhere
- **Day-one backward compat** — no history → current static values as fallback
- **One budget knob** — `budget_per_story_usd` is the primary operator control;
  phase allocation and per-agent limits derive from it

## Definition of Done

- `forge init` produces a working config that a new user can run immediately
- Adaptive assignment makes per-story model decisions from the available pool
- Iteration/timeout limits scale with complexity and learned agent behavior
- Stuck agents are detected and terminated early (not at the budget wall)
- Escalation history auto-promotes models after repeated failures for a tier
- A sprint of 5 stories with adaptive enabled requires zero manual knob-tuning
