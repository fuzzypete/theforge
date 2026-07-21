# Refusal-Capability

**Status:** Doctrine.
**Scope:** Product constraint on when TheForge should decline to act rather than proceed.
**Relation to other documents:** Doctrine names *what good looks like*. ADRs make specific structural decisions; conventions encode rules; plans describe mechanisms. This document is upstream of all three — every ADR and every feature should be evaluable against it. It is the sibling of [`compound-engineering.md`](compound-engineering.md); the two axes are distinct on purpose (see "Refusal-capability and compound engineering are siblings" below).

---

## The principle

A sophisticated SDLC orchestrator is not one that guesses faster — it is one that knows when a thing is not ready to be implemented and refuses to proceed. Refusal-capability is the property that makes the rest of the system trustworthy.

TheForge is not valuable because it produces more attempts per hour. It is valuable when it can decline to spend on unready work and say *why* in a legible, typed form: refusing symptom bugs without diagnosis, stories without observable acceptance criteria, ambiguous scope, and work with unresolved dependencies. Every other capability — routing, compounding memory, autonomy — assumes the substrate of "what got built" was built from work that was actually ready. Refusal is what makes that assumption hold.

## The failure mode this rules out

> **A system that cannot refuse can be coerced into running garbage, and everything downstream compounds the garbage.**

Without refusal-capability, the only way to answer "is this ready?" is to run it and see what breaks. Unready work enters a sprint, produces a plausible-looking artifact, and the operator discovers the framing was invalid only after spend. Worse: the invalid framing evaporates instead of being captured, so the next similarly-unready issue takes the same path from zero.

Refusal-capability is the product constraint that says: a gate must be able to *stop* with a reason a contributor can read, and that stop must not be silently bypassable. A check that can only be satisfied — never cleanly refused — is not a gate; it is a speed bump the operator learns to drive over. The refusal has to be a first-class outcome, not an error state to route around.

## The mechanism

Refusal-capability is not an abstraction looking for a home. It has a concrete realization in [ADR-0001](../adr/0001-intake-readiness-workflow.md): the intake-readiness workflow makes "this issue is not ready" a first-class operator surface with typed verdicts, rather than a hand-edit loop bolted onto sprint entry.

The load-bearing pieces:

- **Sprint entry is the final readiness check, not the primary place readiness is created.** The refusal-capable gate stays at sprint entry, but readiness is produced deliberately upstream by dedicated commands (`forge shape`, `forge diagnose`, `forge groom`). Refusal stops being a friction paid every time an issue is filed and becomes a signal that routes work to the right producer.
- **Typed verdicts.** The shape gate emits a bounded verdict vocabulary rather than a generic pass/fail, so a refusal names what is missing and which command repairs it.
- **The three-state diagnosis taxonomy.** Bugs are allowed an honest "I don't know the cause yet" state that is neither a refusal nor a green light — the state the rest of the system depends on to avoid false confidence.

## How features are evaluated against this doctrine

A single one-line test, the refusal-axis parallel to compound engineering's "does this leave a reusable trace?":

> **Can this gate refuse cleanly with a typed verdict, or does it force the operator to bypass the check?**

Apply it to every feature that gates, validates, or admits work. If the answer is "it can only pass or hard-error, and a stuck operator's only move is `--force`," that is the doctrine pushing back. A gate whose sole escape hatch is bypass teaches operators to distrust the gate; a gate that refuses with a typed, actionable verdict teaches them to fix the input.

Worked examples of the test in use:

- A shape gate that collapses "no diagnosis" and "diagnosis, cause unknown" into a single refusal forces operators to fake a confirmed cause to get moving. It fails the test: the honest state has no clean verdict, so the operator bypasses. ADR-0001's three-state taxonomy gives the honest state a first-class verdict (`diagnosis_cause_unknown`) and passes it.
- A dependency check that blocks a story on an unresolved `depends_on` but offers no reason string forces the operator to guess whether to wait or override. Naming the blocking dependency turns a bypass into a decision.
- A validation step that raises an untyped exception when an issue is malformed evaporates the *why*. Replacing it with a typed verdict (`needs_type`, `needs_grooming_missing_ac`) makes the refusal legible and routable.

## Refusal-capability and compound engineering are siblings

Two axes, neither subsumes the other. See [`compound-engineering.md`](compound-engineering.md) for the other half.

- **Refusal-capability** preserves *what gets built*. If the system can be coerced into running unready work, the substrate of completed runs becomes untrustworthy. Compound engineering compounds garbage in that world.
- **Compound engineering** preserves *what is learned*. If the system can refuse but not reuse, every refusal is paid for in operator pain rather than amortized into future automation.

The clean division of the worked examples: **ADR-0001 is the refusal-axis example** — it makes "this issue is not ready" a first-class operator surface rather than a hand-edit loop. **ADR-0002 is the compound-axis example** — it makes run history queryable, immutable, and trustworthy as router input. Both axes are doctrinal; ADRs operationalize one or the other (sometimes both). ADR-0002's refusal-to-forget invariant — no operator-facing delete/redact API for run records — is itself a refusal at the substrate layer: the system declines to let failures be erased, so a compounding router cannot be fed a doctored history.

## What this doctrine constrains, what it does not

The constraint surface of refusal-capability:

- **Gate design:** every gate that admits work must be able to refuse with a typed verdict, and the refusal must be legible (names what is missing) and routable (implies a repair path). Bypass may exist as an incident-time fallback, but it must be loud and logged, never the default ergonomic path.
- **Readiness workflow:** readiness is produced deliberately upstream, not clumsily created at the last check. The producer commands (`forge shape`, `forge diagnose`, `forge groom`) are the current realization for issue intake; analogous refusal-capable surfaces for plans and reviews are open future work.
- **Diagnosis honesty:** the system must support an honest "cause not yet identified" state as first-class, distinct from both a refusal and an implementation-ready green light. Collapsing it forces false confidence.
- **Symptom verification:** when a diagnosed bug ships, review verifies the *symptom* no longer reproduces, not merely that the hypothesized cause was addressed. Refusing to conflate "fixed the guess" with "fixed the problem" is a refusal at the review boundary. The [reviewer prompt template](../guides/reviewer-prompt-template.md) makes this concrete: it forces source-of-truth grounding (tree-state proof) *before* analysis and tags every claim with a certainty level, so a reviewer refuses — "I could not verify this" — rather than confabulating a review of a stale or wrong tree.

The constraint surface this doctrine does *not* cover:

- **Mechanical implementation of any refusal gate** — what the shape gate parses, how a verdict maps to a command, what a producer agent emits. Those belong in ADR-0001 and its downstream issues.
- **Specific refusal vocabularies** beyond what ADR-0001 establishes. The nine-item v0.11.x typed-verdict list is the current bound; broadening it is router-epic work, not doctrine.
- **Convention-level rules** — issue body shape, label vocabulary, section ordering. Those belong in [`CONVENTIONS.md`](../../CONVENTIONS.md).

Doctrine constrains *what features should aim for*; ADRs constrain *what implementations must guarantee*; conventions constrain *how work is shaped*. Refusal-capability as doctrine pushes back on "can this check ever say no, and does saying no help?" rather than "which verdict string maps to which command?"

## Out of scope for this doctrine

- **Mechanical implementation of any refusal gate.** Mechanisms live in ADR-0001 and downstream issues; invariants live in ADRs.
- **Specific refusal vocabularies beyond what ADR-0001 already establishes.** The doctrine names typed refusal as a category, not a fixed enum.
- **Combining refusal-capability with compound engineering into a single doctrine.** The two axes are distinct on purpose; each has its own document precisely so a feature can be weighed against one without collapsing it into the other.

## Worked example: ADR-0001 intake readiness

ADR-0001 is the canonical refusal-axis worked example. A concrete trace:

**Before.** "Is this issue ready?" was answered only at sprint entry, as a refusal, with no companion surface to make it ready. Every issue filed under pressure took the same loop: file → sprint → gate refuses → hand-edit → retry. The refusal was correct but paid for in operator friction every single time, and the correction evaporated.

**The typed-verdict vocabulary.** ADR-0001 bounds the shape gate's v0.11.x verdict surface to nine typed outcomes:

```
needs_type
needs_diagnosis
diagnosis_cause_unknown
needs_grooming_missing_ac
needs_grooming_missing_example
needs_grooming_scope_split
needs_operator_action
adr_candidate
duplicate_or_stale
```

Each verdict is a *clean refusal with a route*: it names precisely what is unready and maps to a single recommended operator command. `needs_diagnosis` routes to `forge diagnose`; `needs_grooming_missing_ac` routes to `forge groom`; `adr_candidate` routes to a human writing durable architecture. The gate refuses — but the refusal is legible and actionable, which is exactly the property the one-line test demands. The operator is never left with bypass as the only move.

**The three-state diagnosis taxonomy.** For bugs, ADR-0001 adopts a three-state vocabulary that is the sharpest expression of refusal-capability in the codebase:

1. **No diagnosis present** — symptom-only. Not sprintable. Refused and routed to `forge diagnose` (`needs_diagnosis`).
2. **Diagnosis exists, cause unknown** — symptom documented, hypotheses ruled out, confirmed cause honestly "not yet identified." Investigation-ready, not implementation-ready. This is a **first-class state, not a refusal** (`diagnosis_cause_unknown`). `forge groom` may tidy the body but MUST NOT apply the `ready` label — the invariant that only diagnosis-with-confirmed-cause bugs can reach sprint-ready.
3. **Diagnosis with confirmed cause** — implementation-ready. Sprint may proceed, and review must verify the symptom no longer reproduces.

The old gate collapsed states 1 and 2, forcing operators to fake state 3. The taxonomy's value is entirely refusal-capability: it gives operators a way to be honest about "I don't know yet" instead of manufacturing false confidence to satisfy a check. State 2 existing at all — a state that is neither a green light nor a refusal — is the doctrine made mechanical.

## Relationship to other documents

- [`compound-engineering.md`](compound-engineering.md) — the sibling doctrine. Refusal-capability preserves *what gets built*; compound engineering preserves *what is learned*. The two cite each other across every ADR.
- [ADR-0001](../adr/0001-intake-readiness-workflow.md) — intake readiness as the canonical refusal-axis worked example: typed verdicts, the three-state diagnosis taxonomy, and the "sprint entry is the final check, not the primary place readiness is created" principle.
- [ADR-0002](../adr/0002-audit-substrate-and-queryable-run-history.md) — audit substrate as a compound-axis example whose refusal-to-forget invariant (no operator-facing delete/redact API) is itself a refusal at the substrate layer, keeping the compounding router's input trustworthy.
- [`CONVENTIONS.md`](../../CONVENTIONS.md) — the "TheForge's core property is refusal-capable execution" North Star, which now points at this doctrine for the full treatment.
- [Reviewer prompt template](../guides/reviewer-prompt-template.md) — the review-trust artifact for the review boundary: a verification-gated reviewer prompt (tree-state proof before analysis, `[VERIFIED]`/`[INFERRED]`/`[SPECULATIVE]` per-claim tags, anti-flattery framing) that makes confabulation more expensive than admitting ignorance.
- Future ADRs likely to cite this doctrine: refusal-capable plan and review surfaces, the adaptive-router trust model, and autonomy boundaries (autonomy is only safe on top of a substrate the system could have refused to build).
