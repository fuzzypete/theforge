# Adaptive Assignment

Adaptive assignment is TheForge's current routing system for choosing eligible
models and reviewers from recorded evidence. The canonical design boundary is:

- **ADR-0006** defines what historical signals may influence routing, how they
  are admitted, and what every routing decision must record.
- **ADR-0002** defines what telemetry is authoritative enough for ADR-0006 to
  consume.

This guide is the operator-facing summary of that system. It does not replace
either ADR; it connects the pieces into one current model.

## The system in one sentence

Static config decides **eligibility**. Adaptive assignment decides
**preference within the eligible pool** from admissible audit telemetry, with
recovery paths, taint exclusion, bounded exploration, and a recorded
`routing_decision` that explains the result.

## Trust boundary: authoritative telemetry vs. derived views

Per ADR-0002, adaptive assignment reads only from the audit substrate:

- **Authoritative:** native-provenance per-run records and coordinator-written
  native substrate rows.
- **Derived/queryable, not authoritative:** `.forge/audits/index.sqlite`,
  `history.jsonl`, `assignment_history.yaml`, `forge_audit.yaml`, and any other
  rebuildable cache or export.
- **Advisory only:** LLM summaries, postmortems, freeform comments, and other
  prose. They may motivate future structured tags, but they do not route.

The router trusts the structured audit substrate, not convenience views and not
unstructured narrative.

## Eligibility first, preference second

Adaptive assignment never invents a candidate outside operator policy.

- CLI flags and explicit `forge.yaml` settings define the eligible pool.
- The adaptive layer reranks candidates inside that pool.
- Deprioritization is sort-after, not filter-out: a weakly ranked eligible
  candidate still runs when no better-standing candidate is available.

This is the core ADR-0006 distinction: history affects preference, not
eligibility.

## Which signals may move routing

ADR-0006 requires every routing-weighted signal to be:

1. Mechanically recorded by coordinator code.
2. Complete over attempts, including failures.
3. Above its configured sample floor.
4. Recency-weighted rather than lifetime-cumulative.
5. Read from schema-stable substrate fields.
6. Admitted per role, not assumed to transfer across roles.

That yields four practical buckets:

- **Directly routing-safe:** current-run facts and trusted structured audit
  fields.
- **Routing-safe after aggregation:** rates and trends such as success rate,
  reviewer completion, reviewer value, role reliability, or escalation history,
  once the admissibility gates above pass.
- **Advisory only:** summaries, narrative RCA, freeform comments, or any signal
  whose quality interpretation is not yet validated.
- **Forbidden:** unknown future schema, raw logs, ad-hoc runtime state outside
  the substrate, and any signal with untraceable provenance.

The important boundary is that **LLM prose never routes directly**. If a prose
pattern matters, ADR-0006 clause 6 requires it to be promoted into a structured,
audited substrate tag first; only the admitted tag may later carry routing
weight.

## Taint, recovery, symmetry, and exploration

Adaptive assignment is allowed to learn only from trustworthy history:

- **Tainted-run exclusion:** runs that fail their own trust checks are kept in
  the audit record but excluded from routing aggregates. They persist; they do
  not teach.
- **Recovery / symmetry:** every adaptive ratchet needs a return path. Recency
  decay is the universal passive recovery path, and discrete promotions or
  demotions need explicit inverses.
- **Exploration:** challenger sampling is the one bounded off-policy exception.
  It is recorded explicitly in `routing_decision`, gated by its own sample
  floors, and can be disabled for deterministic dogfood baselines.

## Explainability surface: `routing_decision` first

Every adaptive decision writes a top-level `routing_decision` block into the
per-run audit record. That block is the canonical explanation contract:

- selected model(s) and final rationale,
- eligible and excluded candidates with canonical exclusion reasons,
- consulted signals with raw value, weighted value, sample-floor result, and
  taint-aware counts,
- adaptive mechanisms with tri-state outcomes,
- exploration mode and per-role score-policy details.

`forge explain` and the assignment summary surface from #270 are **read-only
views over that recorded block**. They are not separate audit sources and they
do not recompute routing from live profiles. When operator and audit views
disagree, the per-run native audit record wins.

## Major v0.13 adaptive mechanisms

The v0.13 adaptive system spans these major mechanisms and issue owners:

| Issue | Mechanism | Role in the system |
|---|---|---|
| #1388 | Reviewer completion tracking | Completeness-safe reviewer reliability signal |
| #1392 | Recency weighting | Shared stale-evidence recovery path |
| #1489 | Role-wide routing | Per-role admissibility beyond dev |
| #1443 | Reviewer value reranking | Structured comparative reviewer signal |
| #155 | Domain-aware routing | Preference within eligible pool from domain evidence |
| #158 | Dev pre-promotion from capability profiles | Bounded history-driven preference shift |
| #325 | Exploration / challenger sampling | Recorded bounded exploration |
| #1851 | Trust-status marker | Structured taint evidence on runs |
| #1852 | Tainted-run exclusion | Exclude failed-trust runs from aggregates |

Related follow-ons also exist, notably #1389 for the symmetry invariant and
#270 / #1391 for the recorded explanation surface.

## Historical note

[`docs/vision/self-adapting-router.md`](../vision/self-adapting-router.md)
captures the pre-ADR design discussion that led here. Treat it as historical
vision, not as the current routing contract. For current behavior, use this
guide plus ADR-0006 and ADR-0002.
