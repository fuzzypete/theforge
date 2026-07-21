# ADR-0006: Adaptive Router Trust Boundary

- **Status:** Proposed
- **Date:** 2026-07-20 (proposed)
- **Deciders:** Peter Wickersham (project lead), with iterative agent review
- **Affected milestones:** v0.13 (adaptive trust and routing — this ADR is the milestone's design nucleus), v0.14+ (cost-tiered generation consumes the same rules)
- **Related issues:** #1536 (v0.13 capture; this ADR promotes items 1–5), #1391, #1388, #1389, #1392, #1443, #1489, #158, #170, #325, #1387, #1534 (Gemini wrong-tree incident)
- **Related documents:** ADR-0002 (`0002-audit-substrate-and-queryable-run-history.md`) — the substrate-side trust boundary this ADR consumes; `docs/vision/self-adapting-router.md`; `docs/vision/compound-engineering.md`; `docs/vision/refusal-capability.md`

---

## Context

ADR-0002 settled the substrate side of adaptive routing: per-run records with
native provenance are authoritative, derived views are queryable but not
authoritative, and LLM-generated summaries advise but never decide. Its clause
4 ("What the router may trust") names the router's read surface — per-run
records, aggregations over them, schema-versioned fields with a release floor —
and stops there. It deliberately does not say what the router is allowed to
*do* with that evidence.

v0.13's theme is adaptive trust and routing: let TheForge learn from history
without letting mushy or stale signals drive mechanical decisions. The
milestone's open issues each add one adaptive mechanism — reviewer completion
tracking (#1388), recency weighting (#1392), promotion/demotion symmetry
(#1389), explainability (#1391), role-wide routing (#1489), escalation
learning (#158), exploration (#170, #325), reviewer quality signals (#1443).
Without a shared trust model, each of those is decided ad-hoc inside its
implementing PR — exactly the failure mode ADRs exist to prevent — and the
milestone ships disconnected featurelets rather than a coherent adaptive
system.

The failure modes this ADR rules out are not hypothetical. Each has already
been observed in dogfood:

- **Survivorship bias.** Reviewer profiles are updated only from attempts that
  returned parseable verdicts; timeouts, crashes, and parse failures evaporate
  from the record (#1388). A signal built from a series that silently drops
  its failures is flattering, not informative.
- **Stale evidence poisoning.** `get_dev_success_rate` is a lifetime
  cumulative average. 185 pre-stability runs at 3% success permanently
  deprioritize a model whose recent performance is fine (#1392). On a project
  that is itself improving, equal-weight history is the wrong shape.
- **One-way ratchets.** Promotion mechanisms exist with no symmetric demotion
  or recovery path; audit data shows ~91% of dev assignments going to
  strong-tier models when the score band predicts 37% (#1387, #1389). A
  system that can only learn caution and never unlearn it converges on
  maximum spend.
- **Unexplainable decisions.** "Why is ChatGPT never a reviewer?" required
  reading `assignment.py` end-to-end. The audit trail records which model ran,
  not why it was selected over the alternatives (#1391).
- **Evidence from untrusted runs.** The Gemini wrong-tree incident (#1534): a
  reviewer reviewed a stale checkout. If that review's outcome feeds routing
  weight, the router learns from an event that did not happen the way the
  record implies.

ADR-0002 is the compound-axis document: it guarantees evidence persists. This
ADR is its router-side counterpart: it defines when persisted evidence is
*admissible* — safe enough to move a mechanical decision — and what every
adaptive decision owes the operator in return. It is also a refusal-capability
document (`docs/vision/refusal-capability.md`): the router refusing to act on
insufficient, stale, biased, or tainted evidence is the same trust property as
the shape gate refusing an ungroomed issue, applied to routing.

## Decision

### Headline principle

> **The router routes on evidence, and evidence has admission criteria. A
> historical signal may carry routing weight only if it is mechanically
> recorded, complete, sufficiently sampled, recency-weighted, and produced by
> a run that passed its own trust checks. Adaptive signals adjust preference
> within operator-defined eligibility — never eligibility itself. Every
> adaptive influence has a recovery path, and every routing decision is
> reconstructable from the audit record.**

### 1. Eligibility and preference are different layers

Static configuration defines **eligibility**: which agents exist, which are
enabled, which have working auth and transport, which tiers they occupy, and
any explicit operator overrides. Adaptive signals define **preference**:
ordering and selection *within* the eligible pool.

- Adaptive mechanisms MUST NOT add a candidate that static configuration
  excludes, and MUST NOT permanently remove a candidate that static
  configuration includes.
- Adaptive deprioritization is a **sort-after, never a filter-out**. A
  deprioritized candidate is still selected when no better-standing candidate
  is available (the existing dev-side fallback pattern, extended to all
  roles).
- The override hierarchy is: CLI flags, then explicit `forge.yaml`
  configuration, then the adaptive layer, then built-in defaults. Explicit
  config always wins over learned preference; the adaptive layer optimizes
  within operator-set bounds, it does not negotiate them.

This is the router-side restatement of "LLMs propose, the coordinator
decides": history proposes an ordering; the operator's configuration decides
the universe that ordering applies to.

### 2. Signal admissibility criteria

A historical signal may carry routing weight only when it satisfies **all** of
the following. A signal failing any criterion is advisory at most (clause 3).

1. **Mechanical provenance.** The signal is recorded by coordinator code from
   an observed event (exit status, timeout, parse result, cost, duration,
   verdict presence). No LLM judgment sits in the measurement loop. This is
   ADR-0002 clause 5 applied at admission time.
2. **Completeness.** The recording path captures **every attempt, including
   failures**, at the moment of the attempt. A series that only records
   successes is survivorship-biased and inadmissible until its recording path
   is fixed — and admissibility begins from the fix, not retroactively over
   the biased history (#1388 is the first application: reviewer
   `completion_rate` computed over all attempts).
3. **Sample floor.** Below a configurable `min_runs` of observations for the
   (signal, role, model) slice, the signal carries no weight and routing
   falls through to static tier/budget logic. Cold start is a static-routing
   condition, not a low-confidence adaptive one.
4. **Recency weighting.** The signal consults a decayed or windowed view of
   history, not a lifetime cumulative aggregate. One weighting mechanism is
   shared across all profile-derived rates (#1392) — not per-call-site
   bespoke logic — and both the raw and weighted values are recorded wherever
   the weighted value is used (clause 7).
5. **Schema stability.** The underlying substrate fields are schema-versioned
   and have shipped in at least one tagged release (ADR-0002 clause 4's
   release floor, unchanged).

Admissibility is **per-role**. Evidence about a model's dev performance says
nothing about its review reliability; the DeepSeek/Gemini case — selected as
reviewers on most sprints while carrying a 0/134 dev success rate, because no
reviewer-side signal existed at all — is the canonical example of the gap
this rule closes (#1388, #1489). A signal admitted for one role is not
thereby admitted for any other.

### 3. Signal classes: the four-bucket taxonomy

Applying clause 2 to the signals the system records today or has committed to
recording in v0.13 sorts every input into one of four buckets. The bucket
determines what interpretation, if any, the signal is allowed to carry into a
mechanical routing decision.

**A. Directly routing-safe** — admissible without further aggregation, because
each is a single observed fact with mechanical provenance:

- Native audit records and schema-versioned substrate fields from a supported
  reader version (ADR-0002 clauses 1 and 4).
- Deterministic aggregations already materialized over native records.
- Current-run facts produced by deterministic coordinator code: model
  identity, role, phase, transport, cost, duration, final outcome, timeout,
  retry/iteration count, parse success, gate result, complexity score and
  band, slug/domain tags recorded by preflight.

**B. Routing-safe after aggregation** — admissible only once clause 2's
confidence gates (sample floor, recency, completeness, per-role, schema
stability) are met, because the signal is a *rate* or *trend* rather than a
single fact:

- Success/failure rate by model, role, domain, or complexity band.
- Reviewer completion rate, parse-error rate, and per-role latency (#1388,
  #1489).
- Transport reliability and escalation rate.
- Challenger/exploration outcomes (clause 8).
- Mechanically-computed comparative signals — e.g. unique-P1 rate across a
  review pool (#1443) — provided the comparison itself is coordinator code
  over structured findings, not an LLM judging quality.

**C. Advisory-only** — may inform prompts, operator review, future issues, and
structured-signal *proposals* (clause 6), but MUST NOT move a mechanical
routing decision in their current form:

- LLM summary content (`.forge/knowledge/summaries/`) — ADR-0002 clause 5,
  restated; promotion path in clause 6.
- Knowledge summaries, postmortems, narrative RCA text, freeform review
  summaries, human issue comments, rationale strings.
- Any quality judgment lacking ground truth: finding "quality", review
  "goodness", false-positive rate. `avg_findings` is mechanically counted but
  its *interpretation* as a quality signal is unvalidated; it stays advisory
  until a corroboration mechanism exists (out of scope, below).

**D. Forbidden mechanical inputs** — MUST NOT touch routing at all, in any
form, because their provenance cannot be traced to trusted structured
telemetry:

- Unknown future schema fields read by an older router (ADR-0002 clause 4:
  skip with a warning, do not guess).
- Raw log prose and unstructured model self-assessments.
- Local runtime state outside the audit substrate (e.g. `.forge/runs/`
  execution state).
- GitHub labels by themselves.
- Any signal whose provenance cannot be traced to a trusted structured record.

This classification is **applied doctrine, not a closed list**. A new signal
enters bucket A or B by passing clause 2, not by analogy to an entry above; a
signal that cannot be traced to mechanical provenance lands in C or D no matter
how useful it looks.

### 4. Tainted runs don't teach

A run that failed its own trust checks — a reviewer whose tree-currency proof
failed, a run whose gate evidence is known-invalid, any run the v0.12 trust
mechanisms flag as unsound — MUST NOT contribute routing weight. Its record
persists in the substrate unaltered (ADR-0002's refusal-to-forget invariant
is untouched: the substrate remembers everything), but routing aggregates
exclude it, and the exclusion is visible in the record.

The Gemini wrong-tree incident (#1534) is the canonical case: a review
verdict produced against a stale checkout is not evidence about the
reviewer's judgment, the story's difficulty, or anything else the router
weighs — treating it as evidence injects noise dressed as signal. Concretely,
this means run records carry (or are joinable to) a trust-status marker, and
every aggregate the router consumes filters on it. Where v0.12 trust proofs
do not yet exist for a run type, its records are admissible by default —
taint requires an affirmative failed check, not the absence of one.

### 5. The symmetry invariant: every ratchet has a return path

Every adaptive mechanism that moves routing in one direction — promote,
escalate, deprioritize, exclude-from-preference — MUST have a defined,
tested mechanism that moves it back under stated conditions (#1389).

- Recency weighting (clause 2.4) is the universal *passive* recovery path:
  stale evidence decays out of relevance on its own timeline, so no
  deprioritization is permanent by default.
- Discrete ratchets (promotion on escalation history, reviewer
  deprioritization on completion rate, tier promotion of a cheap adapter)
  additionally need an *explicit* inverse: the conditions under which the
  opposite transition fires, recorded with the same audit attribution as the
  forward transition.
- Static-routing structure (the score-band tier table, hard tier floors,
  operator overrides) is exempt — it is not history-driven and is the
  operator's to set.

Enforcement is mechanical, not documentary: a test asserts that each named
promotion path in the routing code has a reachable, test-covered demotion
path, and the audit record notes when a demotion *could have fired but
didn't* (clause 7) so the return paths are observable in production, not
just in fixtures. A PR adding a one-way mechanism without its inverse fails
CI — the invariant is a gate, not advice.

### 6. From prose to signal: the promotion mechanism

When a pattern is observed in LLM summaries or operator experience ("this
kind of bug always escalates"), the path to routing influence is:

1. The pattern is expressed as a **structured tag on the substrate records it
   applies to** — written by a deliberate, recorded action (an operator
   command or a coordinator-owned writer), carrying provenance that names its
   origin (which summaries, which analysis, who or what promoted it).
2. The tag is now a substrate field like any other and must pass clause 2 on
   its own merits — sample floor, recency, completeness over the slice it
   claims to describe — before it carries weight. This moves it from bucket C
   toward bucket B; it is not routing-safe until it clears clause 2.
3. The prose never routes. The tag can, once admissible.

This closes the loop ADR-0002 clause 5 left open: summaries are not a dead
end, they are a *hypothesis source*, and the promotion step is where a
hypothesis becomes measurable. The promotion action itself is auditable — a
tag with no recorded provenance is not admissible.

### 7. The explainability obligation

Every routing decision writes a `routing_decision` block into the audit
record at decision time (#1391), containing at minimum:

- The candidate pool per role, with exclusions attributed to an
  **enumerable** reason vocabulary (`auth_missing`,
  `transport_unavailable`, `tier_mismatch`, `anti_self_review`,
  `phase_eligibility`, `explicit_override_locked`, …) — no free-form strings
  for the canonical set, so refusal reasons are greppable and countable.
- The signals consulted, with **both raw and weighted values** where recency
  weighting applies, plus the sample counts that cleared (or failed) the
  floor.
- Which adaptive mechanisms fired, which were checked and did not fire and
  why — including demotion paths that could have fired (clause 5's
  observability requirement) — and which exploration state applied
  (clause 8).
- The final selection with a single-sentence rationale naming the deciding
  factor.

The block records data the routing pass already holds — it MUST NOT add
agent invocations or new profile reads. The standard is: **a routing
decision that cannot be reconstructed from the audit record alone is a
defect in the router, not a gap in logging.** This is test-enforced as part
of #1391's acceptance criteria, not left to review discretion.
Operator-facing query surfaces (`forge explain`, `--dry-run` forms) are
conveniences built on this block; the block is the contract.

### 8. Determinism, and exploration as the one sanctioned exception

Routing is a pure function of substrate state and configuration: same
evidence, same config, same decision (#158's determinism requirement,
generalized). This is what makes routing decisions reviewable, testable, and
arguable-with.

Exploration runs (#170, #325) are the single sanctioned deviation —
deliberately routing off-policy to generate fresh evidence, because a system
that only exploits its history can never discover that a cheap model got
good. Exploration is admissible only when:

- **Labeled:** the decision is marked as exploration in the
  `routing_decision` block and in telemetry (`challenger` vs `winner`);
  unlabeled off-policy routing is a defect, full stop.
- **Bounded:** at most a configured number of exploration runs per sprint, at
  a configured frequency. The default is a grooming decision, not fixed here.
- **Budget-capped:** exploration spends from the explored tier's budget
  envelope, not the incumbent's.
- **Recoverable:** a failed exploration run retries at the normal-tier
  assignment without consuming the story's outcome — the story is not the
  experiment's casualty.
- **Recorded:** outcomes enter the substrate like any other evidence, and
  where challenger selection is stochastic, the selection made (and the pool
  it was drawn from) is recorded so the run remains fully reconstructable
  after the fact.

### 9. Cost-tier transitions ride the same rules

When cost-tiered generation lands (e.g., a cheap local adapter for some
phases), its per-run cost and outcome evidence flows into the same substrate
surface, and tier transitions are governed by the clauses above with no
special-case policy: promotion of a cheap tier into wider use is a promotion
path (clause 5 — needs an explicit demotion counterpart), gated on admissible
evidence (clause 2 — sample floor, recency, completeness), decided within
operator-set eligibility (clause 1), and explained in the audit record
(clause 7). This clause exists so the cost-tier work, whenever it lands, is
an application of this ADR rather than a renegotiation of it.

## Relationship to the v0.13 issue set

This ADR is the trust model; the open issues are its implementation slices.
The mapping, so grooming can sequence against clauses rather than
re-deriving scope:

| Issue | Implements |
|---|---|
| #1391 router explainability | clause 7 (the load-bearing observability contract; should land first or alongside everything else) |
| #1388 reviewer attempt-completion | clauses 2.1–2.3 (completeness + sample floor) for the review role |
| #1392 recency-weighted success rate | clause 2.4 (the shared weighting mechanism) |
| #1389 symmetry invariant | clause 5 (documentation + CI enforcement) |
| #1489 role-wide routing | clause 2's per-role admissibility, extended beyond dev |
| #1443 reviewer quality signals | clause 3 bucket B's mechanically-computed comparative signals |
| #158 escalation learning | a promotion path under clauses 2, 5, 7, 8 |
| #170 / #325 exploration | clause 8 |
| Cost-tiered generation (future) | clause 9 |

## Out of scope

- **Decay function choice and parameters.** Exponential vs windowed, default
  half-life — implementation decisions defended in #1392's PR, bounded by
  clause 2.4's requirements (documented, deterministic, shared, testable).
- **Reviewer quality ground truth.** Corroboration, false-positive rate,
  finding-accuracy scoring require ground truth the system does not have.
  They stay advisory (clause 3, bucket C) until a dedicated mechanism earns
  them admission; that mechanism is its own future ADR or issue.
- **Optimizer sophistication.** Bandit algorithms, scoring formulas, tie-break
  refinements — any selection algorithm is acceptable if it obeys the
  clauses. This ADR constrains inputs, reversibility, and observability, not
  the arithmetic between them.
- **Cross-repo / global performance bootstrap.** The vision doc's global
  performance table is future work; nothing here blocks it, but per-repo
  evidence is the only admissible kind today.
- **Autonomy boundaries.** Whether to run at all is v0.12 territory
  (gates, refusal verdicts). This ADR governs only how history influences
  *who* runs *what*, given that the work was admitted.
- **The mechanism inventory itself.** Which promotion/demotion mechanisms
  exist is milestone planning; this ADR requires only that each obeys the
  clauses.

## Consequences

### Positive

- The v0.13 milestone has a design nucleus: each issue implements named
  clauses instead of negotiating trust ad-hoc per PR, and new adaptive
  proposals are evaluated by clause rather than by taste.
- Each observed failure mode is foreclosed structurally: survivorship bias
  (2.2), stale poisoning (2.4), one-way ratchets (5), black-box routing (7),
  learning from tainted runs (4).
- Operator sovereignty is preserved by construction: the adaptive layer can
  never override explicit config or permanently exclude an operator-enabled
  model (clause 1), which is what makes turning the adaptive layer *on*
  a safe decision rather than a leap of faith.
- Migration safety falls out for free: with recency weighting admissible
  evidence decays on its own timeline, so `forge migrate-profiles` stops
  being a forced choice between losing history and poisoning routing.

### Negative

- Admission criteria slow signal adoption — deliberately. A promising signal
  waits for a complete recording path, a sample floor, and a release floor
  before it moves a decision. The cost is latency in getting smarter; the
  alternative is routing on flattering or stale data.
- Per-role admissibility means each role's evidence pool fills separately;
  reviewer routing stays static longer than dev routing did.
- The explainability block and dual raw/weighted recording add audit-record
  size and a small maintenance surface to every future routing change.
- The symmetry invariant makes every promotion mechanism roughly twice the
  work (forward path + tested inverse). That is the point, but it is real
  friction on routing PRs.

### Risks

- **Enforcement decay.** The clause-5 CI check or the explainability
  completeness erodes as mechanisms multiply. Mitigated by making both
  test-enforced (#1389, #1391 acceptance criteria) rather than convention.
- **Taint marking depends on trust proofs that are still landing.** Clause 4
  is only as strong as v0.12's trust-check coverage; the default-admissible
  rule means unproven run types contribute weight. Acceptable because the
  alternative (default-taint) would zero out all history, but worth
  re-checking when v0.12's trust surfaces stabilize.
- **The classification calcifies.** Clause 3's four buckets get treated as a
  closed list and new signals are argued in by analogy instead of through
  clause 2. Mitigated by the explicit "applied doctrine, not a closed list"
  language, and by review pushing every new signal through the criteria.
- **Exploration creep.** Exploration frequency/budget knobs drift upward
  until off-policy routing is routine and the "deterministic by default"
  property is fiction. Mitigated by the per-sprint cap and by exploration
  being loudly labeled in every audit record.

## References

- ADR-0002: Audit Substrate and Queryable Run History — the substrate-side
  trust boundary; clauses 4 and 5 there are the foundation clauses 2, 3, and
  6 here build on
- `docs/vision/self-adapting-router.md` — the vision this ADR constrains
  (the flywheel, the config hierarchy, cold start)
- `docs/vision/compound-engineering.md` — why evidence must persist and
  compound; this ADR is the guard that keeps compounding from amplifying bad
  evidence
- `docs/vision/refusal-capability.md` — the doctrine axis: the router
  declining to route on inadmissible evidence is refusal-capability applied
  to routing
- #1536 — the v0.13 capture this ADR promotes (items 1–5)
- #1534 — v0.12 capture; the Gemini wrong-tree incident grounding clause 4
- #1387 — post-plan checkpoint; first concrete demotion path and the source
  of the 91%-vs-37% over-allocation data
