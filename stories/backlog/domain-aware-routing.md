---
name: "Domain-aware adaptive routing — match model strengths to story type"
slug: domain-aware-routing
pytest_target: tests/
depends_on: [plan-escalation]
---

# Domain-Aware Adaptive Routing

## Problem

Adaptive assignment routes by complexity (LOW/MEDIUM/HIGH) but ignores what
kind of work the story requires. A simple CSS positioning story and a simple
API endpoint story both score "small" but need very different model strengths.

Observed: progress-dots (CSS layout + dynamic positioning) was assigned sonnet
which struggled for 3 cycles. Codex/GPT models may be stronger at spatial/layout
tasks. We don't know because we never try — the router always picks by tier.

## Solution

### Preflight domain classification

Preflight already reads the story and codebase. Add a domain tag to its output:

- `frontend-layout` — CSS, positioning, responsive design
- `frontend-state` — React state, hooks, context
- `backend-api` — endpoints, middleware, routing
- `backend-data` — database, migrations, queries
- `concurrent` — async, threading, race conditions
- `refactor` — rename, restructure, no new behavior
- `test` — test-only changes
- `docs` — documentation only
- `general` — no strong domain signal

### Complexity score: 1-10

Replace LOW/MEDIUM/HIGH with a numeric score that factors in both scope and
domain difficulty:

- Scope: files touched, test surface, integration points (1-5)
- Domain difficulty: spatial reasoning, concurrency, novel design (1-5)
- Combined: max(scope, domain) + min(scope, domain)/2 → 1-10 range

### Agent strengths matching

The `strengths` field on AgentDef already exists but is ignored. The router
should prefer agents whose strengths match the story's domain tag.

### Escalation memory with domain

Escalation records already track story + outcome. Add domain tag so the router
can learn "opus succeeds at frontend-layout stories where sonnet fails."

## Acceptance criteria

- Preflight outputs a domain tag alongside complexity
- Complexity is a 1-10 score, not LOW/MEDIUM/HIGH
- Backward compat: LOW/MEDIUM/HIGH still accepted, mapped to 3/5/8
- Agent strengths field is used in routing decisions
- Domain tag stored in escalation records
- Existing 3-tier routing works as fallback when domain is "general"
- All existing tests pass
- New tests for domain classification and strength matching
