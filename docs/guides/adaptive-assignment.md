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
**preference among eligible candidates** from admissible audit telemetry, with
recovery paths, taint exclusion, bounded exploration, and a recorded
`routing_decision` that explains the result.

"Eligible" means operator-enabled, not same-tier: the story's base tier is a
prior that measured evidence can override in either direction. See
[What is adaptive and what is not](#what-is-adaptive-and-what-is-not).

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
eligibility. Note that the pool is bounded by operator config, not by the
story's base tier — a cross-tier substitution backed by admissible evidence is
still a preference decision inside operator-set eligibility, which is why
clause 9 permits it.

## What is adaptive and what is not

"Adaptive routing" is several distinct layers with different owners, and
conflating them is the usual source of confusion about why a given model was
selected. The boundary, layer by layer:

| Layer | Owner | Where |
|---|---|---|
| Score → base tier (the score-band table) | Static / operator policy | `src/theforge/routing.py`, [Routing Policy](routing-policy.md) |
| A model's declared `tier` and `capability` | Static / operator policy | model catalog |
| Preference within the eligible tier | **Adaptive** | profile success rate, domain evidence, recency |
| Evidence-qualified substitution across tiers | **Adaptive** (since #2392) | `exploration.py` winner selection |
| Detecting that a declaration looks wrong | Planned (#2308) | reports; does not edit |
| Rewriting declarations or score bands | **Undecided, unimplemented** | — |

Two things follow that are easy to get backwards.

**The tier table is a prior, not a ceiling.** ADR-0006 clause 5 exempts static
routing structure — the score-band table, hard tier floors, operator overrides —
from the symmetry invariant, because it is not history-driven. But clause 9
allows evidence to route *across* those tiers: a model outside the story's base
tier can be selected when it has admissible measured evidence for the routing
key, meets the reliability floor, and has the lower expected completion cost.
Before #2392 this substitution existed but ranked on success rate with cost
consulted only on an exact tie, which meant it ran in one direction only and
concentrated dev selection on the most expensive candidate. It now ranks
cost-first among candidates that clear the floor.

**Substitution runs both ways.** Story #2434 scored 6, resolved to base tier
mid, and dispatched opus (declared strong) as the cost-qualified winner for key
`dev:medium:backend+concurrency+testing` — reliability 0.9077 against a 0.70
floor, estimated completion cost $5.47. The same mechanism routes score-5 and
score-7 work to mid-tier models when the slice evidence supports it. A selection
that disagrees with the base tier is therefore not prima facie a defect.

Consequently, **the routing snapshot in `.forge/routing/` cannot explain a
selection.** It records the outcome, not the substitution that produced it
(#2393). Use `forge explain --story <n>`, which reads the per-run
`routing_decision` block, or the audit record directly. That works while the
story is still running or after `forge stop` killed it, not only once it has
finished: `forge explain` falls back to the in-flight per-story `audit.yaml` and
the resume record, which hold the same block before the run reaches the audit
substrate (#2923).

What no mechanism does today is compare the evidence back against the
declaration that produced eligibility in the first place. A model declared
stronger than it behaves gets ranked below its peers and quietly stops being
selected; a model declared weaker than it behaves is never selected, so produces
no evidence to the contrary, and the declaration makes itself true. #2308 adds
the detection report for this and stops there — it recommends, and the declared
value stays the operator's to set. Whether the system should ever rewrite a
declaration or a score band is an open question about operator sovereignty
(ADR-0006 clause 1), not a pending implementation task; it would need a decision
before it could become an issue.

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

In v0.14, #2392 made winner selection cost-first among candidates clearing the
reliability floor, which is what turned cross-tier substitution into a
bidirectional mechanism rather than a one-way concentration on the most
expensive candidate.

## Historical note

[`docs/vision/self-adapting-router.md`](../vision/self-adapting-router.md)
captures the pre-ADR design discussion that led here. Treat it as historical
vision, not as the current routing contract. For current behavior, use this
guide plus ADR-0006 and ADR-0002.
