# Compound Engineering

**Status:** Doctrine.
**Scope:** Product constraint on what TheForge should and should not optimize.
**Relation to other documents:** Doctrine names *what good looks like*. ADRs make specific structural decisions; conventions encode rules; plans describe mechanisms. This document is upstream of all three — every ADR and every feature should be evaluable against it.

---

## The principle

TheForge should turn every run, failure, review, diagnosis, and operator correction into reusable engineering leverage instead of letting it evaporate after one moment of attention.

That is the through-line. Refusal-capability (the system knowing when *not* to act) makes the substrate of "what got built" trustworthy. Compound engineering (the system reusing what it learned when it *did* act) makes the substrate of "what we know" productive. Both are required. Neither is sufficient alone.

## The failure mode this rules out

> **Non-compounding work costs attention once, then disappears.**

A run fails, the operator diagnoses it, files a fix, ships it — and the cause-of-failure, the diagnosis trail, the operator's correction, and the cost of being wrong all evaporate. The next similar failure starts from zero. The system never gets smarter.

Compound engineering is the product constraint that says: features that leave reusable traces compound; features that don't are net-negative even when they ship correctly, because they consume attention without producing leverage. A bug fix that solves today's incident without making tomorrow's similar incident cheaper is incomplete unless the story is intentionally one-off.

## The mechanism

Compound engineering is not an abstraction looking for a home. It already has a concrete three-layer mechanism in [`docs/plans/knowledge-capture.md`](../plans/knowledge-capture.md):

- **Layer 1 — Capture.** Stop discarding what we already produce. Story text, plan content, plan regeneration lineage, and the structured review-finding registry — all of it currently drops on the floor. Layer 1 preserves it in the per-run audit record. (Raw dev prompts and full agent responses are intentionally out of scope for Layer 1; per `knowledge-capture.md`, those stay in `.forge/logs/` because they can contain unbounded text and unredacted content.)
- **Layer 2 — Summarize.** Distill runs into evidence-backed knowledge: what was attempted, what worked, what failed, what was learned. A bounded LLM task ("summarize, don't decide"), aligned with the principle that LLMs generate artifacts, not process decisions.
- **Layer 3 — Feed forward.** Prior run summaries flow into future context assembly through existing machinery. Agents stop rediscovering the same conventions, patterns, and failure modes.

The substrate that holds these layers durably is governed by [ADR-0002](../adr/0002-audit-substrate-and-queryable-run-history.md). The intake-readiness workflow that captures operator corrections rather than punishing them is [ADR-0001](../adr/0001-intake-readiness-workflow.md). Each is one expression of the doctrine; this document is the doctrine itself.

## How features are evaluated against this doctrine

A single one-line test:

> **Does this leave a reusable trace, or does it cost attention once and evaporate?**

Apply it to every meaningful feature. If the answer is "evaporates and that's fine because the work is intentionally one-off," document the intent explicitly. If the answer is "evaporates and we hadn't thought about it," that is the doctrine pushing back.

Worked examples of the test in use:

- A symptom-only bug entering a sprint via `--force` ships a fix but produces no diagnosis, no failure classification, and no input to the adaptive router. It evaporates. The doctrine demands either a diagnosis path (now formalized in [ADR-0001](../adr/0001-intake-readiness-workflow.md)) or an explicit operator-action one-off marker.
- A reviewer pass that surfaces P1 findings and discards them when the run terminates evaporates. Findings should be persisted as queryable substrate rows (per [ADR-0002](../adr/0002-audit-substrate-and-queryable-run-history.md)) so the rate of recurrence is mechanical, not anecdotal.
- An adaptive-router change that improves first-attempt success but does not record *why* in a form future routers can read is one-shot improvement. It works for this release; it does not compound across the system's lifetime.

## Compound engineering and refusal-capability are siblings

Two axes, neither subsumes the other. From the `project_north_star.md` memory entry (durable artifact pending — see "Companion doctrine" at the end of this document):

> A sophisticated SDLC orchestrator is not one that guesses faster — it's one that knows when a thing is not ready to be implemented and refuses to proceed.

- **Refusal-capability** preserves *what gets built*. If the system can be coerced into running unready work, the substrate of completed runs becomes untrustworthy. Compound engineering compounds garbage in that world.
- **Compound engineering** preserves *what is learned*. If the system can refuse but not reuse, every refusal is paid for in operator pain rather than amortized into future automation.

ADR-0001 is a refusal-axis example: it makes "this issue is not ready" a first-class operator surface rather than a hand-edit loop. ADR-0002 is a compound-axis example: it makes the substrate of run history queryable, immutable, and trustworthy as router input. Both axes are doctrinal; ADRs operationalize one or the other (sometimes both).

## What this doctrine constrains, what it does not

The constraint surface of compound engineering:

- **Feature design:** features should be evaluable by the one-line test above. Features that evaporate by default should require explicit justification.
- **Substrate design:** the audit substrate must support querying signals that future features could reuse. (Operationalized in ADR-0002 clause 3.)
- **Failure handling:** failures are first-class compounding input. ADR-0001's `diagnosis_cause_unknown` state, the postmortem discipline, the `forge diagnose` command, and #1516's staleness check are all expressions of "failure produces reusable knowledge, not just incident response."
- **Operator corrections:** when an operator hand-edits an issue, overrides a gate, or rewrites a plan, that correction should leave a substrate trace that future automation can read. The intake-readiness workflow is the current realization of this for issue shape; analogous mechanisms for plan corrections and review overrides are open future work.

The constraint surface this doctrine does *not* cover:

- **Implementation invariants** — how schemas evolve, what's immutable, what columns are indexed, what an LLM summary is allowed to influence. Those belong in ADRs. (ADR-0002 covers the substrate's invariants; future ADRs will cover routing trust, autonomy boundaries, and review handoff shape.)
- **Convention-level rules** — naming, file layout, commit message shape, label vocabulary. Those belong in [`CONVENTIONS.md`](../../CONVENTIONS.md).
- **Per-feature mechanics** — what arguments a CLI takes, what a specific gate verifies. Those belong in issue acceptance criteria and design plans.

This separation matters because reviewers tend to collapse it. A useful sharpening: doctrine constrains *what features should aim for*; ADRs constrain *what implementations must guarantee*; conventions constrain *how code looks*. Compound engineering as doctrine pushes back on the question "should we build this?" rather than the question "how should this column be indexed?"

## Out of scope for this doctrine

- **Mechanical implementation of any compounding loop.** Mechanisms live in `docs/plans/` (e.g., `knowledge-capture.md`, `forge-storage-layout.md`); invariants live in ADRs.
- **Specific metrics formulas.** The refusal-economics metric (remediation-to-runnable cost ratio) is named in ADR-0002 as a substrate query obligation. Other CE metrics may emerge; the doctrine names them as a category, not a fixed formula.
- **Mandating that every feature compound.** A focused bug fix, a cosmetic CLI polish, or a one-shot config migration is allowed to evaporate; the doctrine demands that we *notice* when something evaporates and decide deliberately, not that we artificially compound work that has no leverage to produce.

## Worked examples already in flight

Concrete instantiations of compound engineering in the current codebase and roadmap. These are not aspirational; they are the doctrine pointing at things that already exist or are landing.

- **v0.10 retheming.** The milestone was originally framed as "autonomy." First dogfood produced operator-correction signal that autonomy was premature: the substrate of trust was not yet built. The roadmap was reshaped — autonomy → compounding memory → workflow determinism + operator trust — so the next milestone could compound on a trustworthy floor. This is compound engineering at the meta level: an operator correction reshaped the roadmap rather than evaporating into a sprint retro.
- **`assignment_history.yaml` → adaptive router.** The earliest concrete compounding loop in TheForge: per-run model performance is recorded, future assignments adjust. Today this is the only routing decision that uses historical evidence. Future router work (a planned ADR before v0.13 implementation) generalizes this pattern.
- **ADR-0001 intake readiness.** Operator corrections to half-formed issues used to evaporate (file → refuse → hand-edit → retry → next time same loop). The intake-readiness workflow captures each correction as a structured action (`forge shape`, `forge groom`, `forge diagnose`) that produces substrate-visible artifacts. The doctrine made the workflow worth defining; ADR-0001 made it concrete.
- **ADR-0002 audit substrate.** The refusal-to-forget invariant — no operator-facing delete/redact API for run records — is compound engineering's most basic mechanical guarantee. Failures cannot be erased to make current state look better. The audit substrate is a record, not a portfolio.
- **#1516 diagnosis-staleness check** *(open, v0.11.0)*. Once shipped, a diagnosis written against commit A will be mechanically detected as stale when commit B lands. The operator's earlier diagnostic work is preserved (compounding) and explicitly invalidated (refusal-capable) rather than silently rotting. Both axes in one feature.

## Categories of compounding output

Not every feature has to produce all of these, but most CE-aligned features produce at least one:

- **Telemetry** — what happened. Per-run records, sprint rollups, indexed dimensions. Authoritative, immutable, queryable.
- **Reasoning** — why it happened. Diagnoses, postmortems, structured review findings. Tied to telemetry via stable IDs.
- **Wisdom** — what we do differently next time. Conventions promoted from repeated lessons, ADRs promoted from architectural decisions, router updates promoted from performance signal.

Layers 1/2/3 of `knowledge-capture.md` map to these categories: capture produces telemetry, summarize produces reasoning, feed-forward elevates reasoning into wisdom over time.

## Metrics this doctrine cares about

These are signals the system should make legible over time. Not all are implemented today; some are obligations on future work.

- **Rediscovery rate.** How often agents re-learn conventions, patterns, or failure modes the substrate already contains. A doctrine-aligned system trends this down.
- **Refusal economics.** The remediation-to-runnable cost ratio (defined as a substrate query obligation in ADR-0002 §6). Makes refusal-capability legible as cost saved rather than friction felt.
- **Operator-correction reuse.** How often a captured operator correction influences a later automated decision. Today this is partially measurable through the assignment-history → router loop; future work will broaden it.
- **Diagnosis trust horizon.** How long a diagnosis stays valid before staleness invalidates it. Once #1516 ships, the staleness check will make this measurable mechanically; the metric over time tells us how aggressive the codebase's churn is relative to the diagnosis-decay rate.

## Relationship to other documents

- [`docs/plans/knowledge-capture.md`](../plans/knowledge-capture.md) — the canonical three-layer mechanism this doctrine names. If the doctrine is "what good looks like," that plan is "how it gets built."
- [`docs/plans/forge-storage-layout.md`](../plans/forge-storage-layout.md) — file format, gitignore, migration sequence. The substrate the mechanism writes into.
- [ADR-0001](../adr/0001-intake-readiness-workflow.md) — intake readiness as a refusal-axis expression of compound engineering.
- [ADR-0002](../adr/0002-audit-substrate-and-queryable-run-history.md) — audit substrate as the trust contract over what compound engineering captures.
- Future ADRs likely to cite this doctrine: adaptive-router trust model (v0.13 prereq), autonomy boundary, commit-centric review handoffs, operator-action work-object type.

## Companion doctrine

Refusal-capability has its own doctrine surface and deserves its own document (`docs/vision/refusal-capability.md` is open future work, promoting `project_north_star.md` from memory). Until that lands, treat the two as a pair: compound engineering and refusal-capability cite each other across every ADR.
