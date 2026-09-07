# Semantic readiness

`REVIEWED_READY` is the intake contract's last state — capture → shaped →
groomed → **ready**. This page states the policy that produces it. The
mechanism (an evaluator, a store, a CLI command) is an implementation detail
and is not what admission consumes.

The one-sentence version: **admission consumes a recorded operator ratification
of one exact document revision, never model output.** That is ADR-0009
clause 6, and everything below follows from it.

## Two separate questions

A document's semantic position is two facts, not one, and they are kept apart
deliberately:

| Axis | Values | Answers |
| --- | --- | --- |
| Requirement | `required`, `not_required` | Does policy require a ratified review before implementation? |
| Evaluation state | `unevaluated`, `awaiting_ratification`, `accepted_concerns`, `reviewed_ready`, `evaluation_failed` | What is on record for the current revision? |

A document with no record reports `unevaluated` whether or not policy requires
a review of it. Whether that absence withholds admission is the requirement
axis's answer, not the state's.

## Which documents require a review

| Type | Lifecycle state | Requirement |
| --- | --- | --- |
| `bug`, `enhancement`, `task`, `spike` | `implementation_ready` | `required` |
| `bug`, `enhancement`, `task`, `spike` | any other state | `not_required` |
| every other type — `epic`, `operator-action`, an untyped document, a manifest file story | any state | `not_required` |

A `not_required` document keeps whatever structural/lifecycle admission result
it already had. Nothing about semantic readiness makes it more or less
admissible.

A document that is not yet `implementation_ready` is already refused on
structural or lifecycle grounds; requiring a semantic review of it would spend
model budget on text that is about to change.

## What each state means for admission

| State | Admission when `required` |
| --- | --- |
| `reviewed_ready` | Admitted. A ratification for this exact revision cleared it. |
| `unevaluated` | Withheld — `semantic_review_not_ratified`. |
| `awaiting_ratification` | Withheld — `semantic_review_not_ratified`. |
| `accepted_concerns` | Withheld — `semantic_concerns_accepted`. |
| `evaluation_failed` | Withheld — `semantic_evaluation_failed`. |

`unevaluated` and `awaiting_ratification` emit **the same reason code on
purpose**. A document carrying concerns no operator has ratified is not refused
*on account of those concerns* — it is withheld because no ratified readiness
exists, which is exactly the fact that also holds when nothing was evaluated at
all. Giving unratified findings their own refusal would make admission react to
raw model output, rebuilding the probabilistic second gate ADR-0009 exists to
prevent.

## Who runs the evaluation

Nobody has to. When admission reaches a document policy marks `required` and no
evaluation is recorded for its current revision, the evaluation is performed
there and then — no `forge review-semantic` keystroke, one issue at a time,
stands between grooming and a recorded review.

The rules that bound it:

- **Only where policy requires it.** A `not_required` document has no
  evaluation performed for it, and neither the presence nor the absence of a
  record changes its admission.
- **Only once per revision, per prompt contract version.** A recurring
  transition against unchanged text reuses the record it already has. A
  recorded *failure* counts as that revision's attempt, so a broken evaluator
  is not retried on every sprint entry; the document reports the failure and
  stays withheld. Changing the configured model does not buy a second
  evaluation of the same text.
- **Against the revision that occasioned it.** The text evaluated is the text
  the gate just read, so an edit landing mid-run cannot produce a record of a
  revision no admission decision was made against. The same identity is checked
  once more at the end: a sprint re-reads every issue to build the story it
  dispatches, and a story whose revision moved between admission and that read
  is withheld rather than dispatched on a decision made about text that no
  longer exists.
- **Once, or not at all.** Scheduling is serialized per revision. When a peer
  holds that serialization it is already doing the work, so this side defers.
  When the serialization cannot be established at all, nobody is doing the
  work: that is an evaluation that could not be attempted, and it is recorded
  as `evaluation_failed` rather than run unserialized.
- **However the document became runnable.** An issue that entry remediation
  repairs mid-run is admitted on the same terms as one that was well-shaped to
  begin with — the repair makes it policy-required, and the evaluation is
  scheduled then rather than deferred to the operator's next invocation.
- **Never a ratification.** Automatic invocation changes *whether an evaluation
  happens*, never what a result means. A raised finding still withholds
  admission until an operator ratifies it, exactly as when the evaluator is
  invoked by hand.
- **Fail-closed.** An evaluation that fails, times out, is refused, produces
  unrecognised output, or cannot be attempted at all is recorded as
  `evaluation_failed`. No failure mode leaves a document reading as
  evaluated-clean — including a failure of the audit store itself, which is
  reported as `evaluation_failed` rather than read as an absence of concerns.

`forge review-semantic` is unchanged and still works for any document,
including one policy does not require a review of, recording its result on the
same terms.

Automatic evaluation runs where budget is already being committed — sprint
query-mode admission and manifest issue admission. `forge status --ready` is a
status command and spends nothing: it reports the records those paths produce,
so an unevaluated issue is listed as withheld rather than evaluated on the spot.
`forge sprint --dry-run` is a preview on the same terms: it bypasses admission
entirely, evaluates nothing, and previews the issues an executing run would
consider rather than the subset a review has cleared.

The evaluator runs before a sprint's cost ledger exists, so what admission
spends is *not* counted against `--budget`. It is disclosed on stderr as it
happens — the number of evaluations and their recorded cost — rather than being
silently absent from the sprint total.

The evaluator refuses to reveal output for a revision with no frozen baseline.
Where an operator has frozen one it is used untouched; where none exists the
automatic path freezes an empty baseline marked `automatic`, which asserts
nothing about the document and which a later human `--baseline-defect-id`
freeze supersedes.

## Ratification

A clean evaluation does not produce `REVIEWED_READY` by itself. Readiness
follows an operator ratification, recorded with `forge ratify-semantic`, which
decides **every** concern the evaluation raised:

- **Rejecting** a concern says it does not stand. It passes without further
  challenge on that revision.
- **Accepting** a concern says it is a real defect. It withholds readiness for
  that revision until the document changes.

A partial ratification is refused: an undecided concern would leave admission
reading model output nobody ruled on.

An evaluation raising no concerns still requires a ratification. The operator's
decision is what admission consumes, and "the model found nothing" is not one.

## Revisions

The revision identity is the `input_digest` over title, body and canonical
type. Every record and every ratification names the revision it was made
against, and neither speaks for any other:

```
groom 2681            -> RUNNABLE                (structural, unchanged throughout)
evaluate 2681         -> 4 concerns   revision=r1
admission 2681        -> not ready — no ratified readiness at r1

operator ratifies: 3 accepted, 1 rejected  (recorded against r1)
admission 2681        -> not ready — 3 accepted concerns withhold readiness at r1

author edits the body -> revision r2
admission 2681        -> unevaluated at r2; the r1 ratification no longer speaks for it
```

Two consequences fall straight out of scoping the derivation to the current
digest, rather than needing an ordering guard:

- **A ratification does not survive an edit.** It would otherwise assert
  readiness about text nobody reviewed.
- **A late evaluation restores nothing.** An `r1` run that completes after `r2`
  exists is simply not a record *of* `r2` — the defect #2666 documents one layer
  up, where an earlier verdict lands after a later one and wins, cannot occur
  here.

A label change that does not change the canonical type leaves the digest, and
therefore a ratification, intact.

## What this stage may not do

- **It never changes the structural verdict.** A defect semantic review catches
  produces a concern. It never becomes a new structural rule, and the
  `ShapeVerdict` a document receives is identical with and without any of this.
- **It is consulted only after structural admission passes.** A structurally
  refused document is refused structurally, with structural reason codes.
- **`--force` does not override it.** `--force` is an escape hatch over shape
  refusals. `unevaluated`, `awaiting_ratification`, `evaluation_failed` and
  `accepted_concerns` all stay withheld under it, on the same grounds as the
  `operator-action` label: these are operator decisions, not shape findings.
  `forge sprint --force` reports the two partitions under separate banners —
  shape-flagged issues as force-admitted, semantically withheld ones as still
  withheld — so the escape hatch never reads as covering both.

## Where it is enforced

One derivation, three consumers, so the surface that advertises work and the
gate that admits it cannot drift:

- `forge sprint` query mode, via the sprint-entry shape gate.
- `forge sprint` manifest mode, for `{issue: N}` entries.
- `forge status --ready`.
