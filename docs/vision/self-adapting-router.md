# Vision: Self-Adapting Model Router

Captured 2026-03-23 from design discussion on adaptive routing evolution.

> **Historical design note.** This document predates ADR-0002 and ADR-0006.
> It is retained as background on the direction that led to adaptive
> assignment, not as the current routing contract. The current system is
> documented in [Adaptive Assignment](../guides/adaptive-assignment.md) and the
> ADRs it cites. In particular, references below to a direct
> `query_escalation_memory()`-style router are superseded by the current
> admissibility, taint, recency, and `routing_decision` rules.

## The Problem With Config

Today's forge.yaml is ~100 lines of model configuration: reviewer profiles,
timeout tiers, iteration caps, tier mappings, budget caps. Every knob is an
admission that the system can't figure it out itself. Users overconstrain,
underprovision, or cargo-cult bad patterns because there's no guidance.

The ideal forge.yaml:

```yaml
project: hdp
budget_usd: 50
```

Everything else discovered, adapted, and tuned automatically.

## The Data Model

Every forge run already produces structured telemetry:

```
{story, domain, complexity, model, phase, outcome, cost, duration, p1_count, cycles}
```

This is a training signal. After N runs, the system has empirical evidence:
- "sonnet succeeds 90% on backend-api stories, 40% on frontend-layout"
- "opus planning passes review 95% first try, sonnet 60%"
- "deepseek review finds unique P1s 5% of the time (not worth the cost)"
- "codex averages 12 iterations on complex reviews, 4 on simple ones"

## The Router as a Query

Today the router is a hardcoded tier table:

```python
PHASE_TIER = {
    "dev": {"LOW": "cheap", "MEDIUM": "mid", "HIGH": "strong"},
    "plan": {"LOW": "mid", "MEDIUM": "mid", "HIGH": "strong"},
}
```

The self-adapting router replaces this with a materialized view:

```python
def select_model(phase, domain, complexity):
    history = query_escalation_memory(phase, domain, complexity)
    if history.has_sufficient_data():
        return history.best_performing_model()
    return default_tier_table[phase][complexity]  # cold start fallback
```

No config file. The "configuration" IS the query result — computed from
observed performance across all prior runs.

## Domain Classification

Preflight already reads the story and codebase. Adding a domain tag is
zero extra cost:

- `frontend-layout` — CSS, positioning, responsive design
- `frontend-state` — React state, hooks, context
- `backend-api` — endpoints, middleware, routing
- `backend-data` — database, migrations, queries
- `concurrent` — async, threading, race conditions
- `refactor` — rename, restructure, no new behavior

Domain + complexity (1-10 scale) gives the router two axes to query against.

## Adaptive Parameters

Model selection is just the first parameter. Every operational knob should
adapt based on observed outcomes:

| Parameter | Static today | Self-adapting |
|-----------|-------------|---------------|
| Dev model | forge.yaml tier | Best model for this domain+complexity |
| Plan model | forge.yaml | Escalate after N rejections |
| Reviewer pool | forge.yaml | Drop reviewers that never find unique P1s |
| max_iterations | Hardcoded per agent | Scale with story complexity |
| timeout_seconds | Hardcoded per tier | Scale with observed phase duration |
| max_review_cycles | Hardcoded (3) | Reduce for simple, increase for complex |
| max_dev_iterations | Hardcoded (3) | Based on historical iteration counts |

## The Config Hierarchy

When the user DOES want control, explicit config always wins:

1. **CLI flags** (`--dev-model opus`) — highest priority, this run only
2. **forge.yaml explicit** — ceiling/override, persistent
3. **Adaptive layer** — adjusts within ceilings, learned from data
4. **Built-in defaults** — cold start, lowest priority

This is the same pattern as CSS specificity or K8s limits vs requests.
The user sets bounds, the system optimizes within them.

## Provider Discovery

Even the provider inventory can be auto-discovered:

```python
def discover_providers():
    providers = []
    if shutil.which("claude"):
        providers.append(CLIProvider("claude"))
    if shutil.which("codex"):
        providers.append(CLIProvider("codex"))
    if os.environ.get("ANTHROPIC_API_KEY"):
        providers.append(APIProvider("anthropic"))
    if os.environ.get("OPENAI_API_KEY"):
        providers.append(APIProvider("openai"))
    return providers
```

The only user input: install the CLIs you want, set the API keys you have.
Forge figures out the rest.

## The Flywheel

Every sprint makes the next sprint smarter:

```
Run stories → Collect telemetry → Update performance table → Better routing → Better outcomes → More data
```

This is the real product differentiation — not "run multiple LLMs" (anyone
can do that) but "automatically learn which LLM to use for which task and
get better over time."

## Cold Start

First few runs use sensible defaults (sonnet dev, opus review, opus planning
for complex stories). Within a week of real usage the router has enough signal
to outperform any static config. New projects bootstrap from the global
performance table; per-project overrides accumulate over time.

When two models land in the same tier (same `cost_rank` and capability), the
router breaks the tie on their real per-MTok price rather than on
`models.enabled` list order — so an enabled but never-dispatched model is not
starved by the accident of being listed second. That first dispatch is what
lets a zero-history model accrue a profile entry, after which normal
evidence-based ranking takes over (issue #1617).

A price only participates in that tie-break — and in cost banding — when the
registry records what identity it was true of. An entry named by a vendor
shorthand (the Claude CLI's `opus`/`sonnet`) resolves to whichever concrete
version the vendor currently ships, so its stored figure can describe a model
that has since moved; those entries carry no pricing attribution and the router
treats their price as unknown rather than authoritative (issue #2203).
Attribution comes from pinning the entry to the version its figures describe,
or from an operator declaring the price for that identity in `forge.yaml`.
`forge check-config` lists any enabled model whose price is being ignored.

## What Stays in forge.yaml

Only what forge cannot discover:
- `project:` name
- `budget_usd:` spending cap
- Provider credentials (via .forge/.env, not yaml)
- Explicit overrides when you disagree with adaptive choices
- Workspace config (create command, branch pattern — project-specific)
- Gate command (how to run tests — project-specific)

Everything else: learned.
