# Policy Assertion Provenance

ADR-0006 clause 6 says LLM-generated prose may propose but never routes. This
guide is the counterpart on the intake side: **generated prose never blocks.**

## The problem

Intake checks — preflight, the escalation advisor — sometimes refuse work by
citing a standing policy: "this contradicts a deliberate architectural
decision." That is a legitimate refusal *when an operator made the decision*.
But ratified policy and run-authored rationale are stored as the same
undifferentiated prose, so an intake check cannot tell them apart, and neither
can the operator reading the refusal.

The worked example is issue #1108. A dev agent implementing a documentation
story wrote into the routing-policy source of truth that reasoning effort was
"intentionally NOT score-controlled." No operator decided this and no ADR
recorded it — the axis had simply never been wired, and a documentation pass
codified the absence as doctrine. That sentence then blocked a chartered story
at two independent layers, because nothing recorded the difference between it
and a real decision.

## The two provenance classes

| class | means | may block? |
| --- | --- | --- |
| `ratified` | An ADR clause or a recorded operator decision, **with the reference** | yes |
| `generated` | Written by a run, or recorded with no provenance at all | no |

**An assertion with no recorded provenance is `generated`.** Absence is never
promoted. There is deliberately no third "unknown" class: a class meaning "we do
not know" would sooner or later be treated as good enough to block.

A `ratified` entry that records no `reference` is also treated as generated —
ratification is a claim about a durable decision, and without the reference
there is nothing for an operator to read.

## The registry

Ratification lives in a repo-local, durable file:

```
.forge/policy-assertions.yaml
```

It is resolved against the project root (`ForgeConfig.project_root`), not the
process working directory, so it reads the same from any subdirectory. A missing
file is the normal starting state, not an error: with nothing ratified, every
cited assertion resolves to `generated` and no prose can stop chartered work.

```yaml
version: 1
assertions:
  - id: reasoning-effort-score-controlled
    text: "Reasoning effort is intentionally not score-controlled."
    provenance: generated
    run_id: "a1b2c3d4"
    retracted: true
    retracted_reason: "Contradicted by chartered work in #1108; no operator decision existed."

  - id: coordinator-pure-python
    text: "Coordinator control flow is pure Python; no LLM decides routing."
    provenance: ratified
    reference: "docs/adr/0001-intake-readiness-workflow.md#coordinator-is-pure-python"
    ratified_at: "2026-05-04"
```

| field | meaning |
| --- | --- |
| `id` | Stable handle. Preferred over text matching when an intake check can name it. |
| `text` | The assertion as written in its source. |
| `provenance` | `ratified` or `generated`. Anything else normalises to `generated`. |
| `reference` | Required for `ratified`: the ADR clause or recorded operator decision. |
| `run_id` | For `generated`: which run authored the assertion, when known. |
| `ratified_at` | Optional date the operator ratified it. |
| `retracted` / `retracted_reason` | A retracted assertion loses blocking authority regardless of class. |

### How a citation is matched

An intake check cites an assertion by quoting it and naming where it read it.
Matching is deterministic Python, in this order:

1. **`id`** — exact, case-insensitive.
2. **Normalized text** — lowercased, punctuation collapsed to spaces.
3. **Token similarity** — Jaccard overlap of content words at or above 0.7, so a
   reworded quote of a ratified assertion still resolves as ratified. Negations
   (`not`, `never`, `no`) are never treated as noise words: an assertion and its
   negation are different assertions.

The threshold is set high on purpose. A false positive here grants blocking
authority to prose that was never ratified — the exact failure this mechanism
exists to prevent. A near miss is not silently dropped: it becomes a
**ratification candidate**, so the operator sees it.

## What intake does with it

### Preflight

A `BLOCKED` verdict now states `blocking_basis`. Only
`blocking_basis: policy_assertion` is adjudicated. Missing credentials, a direct
specification contradiction, and an absent external dependency block exactly as
they did before — those refusals contain no policy claim.

For a policy-founded `BLOCKED`:

- **At least one cited assertion resolves as ratified** → the refusal stands, and
  `state.error` names the assertion and its provenance class, so the operator can
  see which kind of authority stopped the work without reading git history.
- **Otherwise** → downgraded to `PROCEED`. The conflict is recorded as a
  **retraction candidate** (the assertion is contradicted by chartered work and
  carries no operator decision), and — when the registry had never heard of the
  assertion — a **ratification candidate**, so the demotion to advisory is
  visible rather than silent.

A `BLOCKED` that cites no assertion but whose reason still claims a standing
decision ("intentionally not…", "already decided", "by design") is adjudicated
the same way, so a refusal cannot escape the check by omitting the citation
field. The phrase list is narrow and matches decision *claims* only — a blocker
that merely mentions "architecture" or "policy" while blocking for an unrelated
hard reason is left alone.

### Escalation advisor

The advisor may cite policy assertions per option and at report level. The
coordinator resolves every citation against the registry — the advisor's own
`claimed_provenance` is evidence for the operator, never an input to authority.
The rendered advisory labels each assertion with its decided class and, for
generated or unmarked ones, says plainly that it carries no blocking authority
and names it as a retraction or ratification candidate.

### `forge shape`

A story that carries acceptance criteria is asking for chartered work to be
implemented. Explaining the decision behind that work — under a `## Decision` or
`## Trade-offs` heading — no longer routes it to `adr-candidate`. An explicit
`adr` label or an ADR-marked title still wins: that is the operator's own
classification, not a shape the body happened to have.

## Operator workflow: ratifying an assertion

When a run surfaces a ratification candidate, decide which it is.

**It is a real decision.** Record it durably first — an ADR clause, or an
operator decision written where you keep them — then add the entry:

```yaml
  - id: <stable-handle>
    text: "<the assertion, as written in its source>"
    provenance: ratified
    reference: "docs/adr/00NN-....md#<clause anchor>"
    ratified_at: "YYYY-MM-DD"
```

The reference is not decoration. Without it the entry is treated as generated,
because a ratification nobody can read is not a ratification.

**It is not a decision.** Retract it: fix the prose at its source, and record the
retraction so the same sentence cannot be re-cited later.

```yaml
  - id: <stable-handle>
    text: "<the assertion, as written>"
    provenance: generated
    run_id: "<the run that authored it, when known>"
    retracted: true
    retracted_reason: "<why — e.g. contradicted by chartered work in #NNNN>"
```

Both edits are ordinary commits, so the marking is auditable by construction.

## Where it shows up in the audit trail

Every run's `preflight` audit block and `preflight.yaml` artifact carry:

| field | meaning |
| --- | --- |
| `blocking_basis` | Which kind of blocker a BLOCKED verdict declared |
| `policy_assertions_cited` | What the classifier cited, as it gave it (advisory) |
| `policy_assertions_resolved` | The same assertions with their decided provenance class and match basis |
| `policy_retraction_candidates` | Assertions contradicted by chartered work with no operator decision |
| `policy_ratification_candidates` | Cited assertions the registry never recorded |
| `policy_blocking_authority` | Whether a ratified assertion upheld the refusal |
| `policy_adjudication` | The full adjudication record |

These arrived in record schema **v33**. Older records read with empty
policy-provenance fields: a v32 run's preflight cited nothing structurally and
no provenance was ever weighed, and empty states that absence rather than
inventing citations.

The fields survive the batch-preflight cache and the resume sidecar, so a
cached or resumed refusal names its assertion the same way a live one does.

## Scope

This is not a general provenance system for documentation. It covers exactly the
assertions intake cites when refusing or escalating work — the ones that are
load-bearing because a check can point at them and stop a story.
