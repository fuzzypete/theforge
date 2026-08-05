# TheForge Vision

TheForge is a bet about where agentic development is going: code generation is
becoming a commodity, and the durable value sits in the layer that decides
**when work should run, proves what happened, and learns from the record**.
This page is the map; the doctrine and decisions live in the linked documents.

## Philosophy

1. **Determinism where it counts.** Models plan, implement, and review; Python
   decides. Every retry, gate pass, escalation, and routing choice is made by
   code against recorded evidence. If a model is deciding whether to retry,
   pass a gate, or escalate, the architecture is wrong.
2. **Refusal is a capability, not a failure.** A system that knows when it is
   *not* ready to run — and says why, in a machine-readable, queryable form —
   is more trustworthy than one that guesses faster. The end state:
   *a competent stranger trusts TheForge's refusals more than its output.*
   Doctrine: [Refusal as Capability](vision/refusal-capability.md).
3. **Compound engineering.** Every run should leave the system smarter:
   telemetry (what happened), reasoning (why), and wisdom (what to do
   differently) accumulate in a substrate that cannot be quietly erased.
   Doctrine: [Compound Engineering](vision/compound-engineering.md).
4. **Mechanical guarantees over prompt-side rules.** Constraints on agents are
   enforced by code — sandboxes, gates, schema validation, allowlisted
   verification commands — never by asking a model nicely. Prompt-side rules
   are suggestions; the architecture must not depend on them.

## Where the system is (v0.13)

The release sequence has been building one property at a time:

- **v0.11 — memory.** The SQLite audit substrate became the authoritative,
  append-only, queryable run history and the runtime read path
  ([ADR-0002](adr/0002-audit-substrate-and-queryable-run-history.md)).
- **v0.12 — failure evidence.** Unattended operation requires that failures
  classify, preserve, and diagnose themselves; the failure-evidence cluster
  landed so autonomy isn't confidently doing more unattended wrongness.
- **v0.13 — adaptive payoff.** With durable history and clean failure signals,
  routing became a real learning loop: complexity-score buckets, plan-aware
  tier selection, escalation-history learning, domain-aware matching, and
  bounded exploration of previously-benched models, all inside an explicit
  trust boundary ([ADR-0006](adr/0006-adaptive-router-trust-boundary.md)).

How it all fits together mechanically — both lifecycles, the module map, the
trust boundaries — is in [Architecture](architecture.md).

## Where it is going

Roadmap truth lives in the
[GitHub milestones](https://github.com/fuzzypete/theforge/milestones), not in
this file. The standing sequence: knowledge feed-forward (completed runs
become advisory memory for future agents), intake autonomy (the gate becomes
a router: capture → shaped → groomed → ready without operator keystrokes),
then a frozen, portable 1.0 surface a stranger can point at their own repo.

## Reading order for newcomers

1. [README](../README.md) — what it is, quickstart.
2. [Architecture](architecture.md) — how a sprint and a story actually run.
3. [Refusal as Capability](vision/refusal-capability.md) — why the intake gate
   is the product, not friction.
4. [Compound Engineering](vision/compound-engineering.md) — why the audit
   substrate is append-only.
5. The [ADRs](adr/) — the decisions, with their evidence and corrections
   preserved inline.

Historical design-capture documents (kept for provenance, superseded in
places by shipped work — each carries its own status banner):
[Self-Adapting Router](vision/self-adapting-router.md),
[Cost-Tiered Generation](vision/cost-tiered-generation.md),
[Agent Intelligence](vision/agent-intelligence.md).
