# ADR-0009: Typed Intake Contract

- **Status:** Proposed
- **Date:** 2026-08-23
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** v0.16.0
- **Supersedes in part:** ADR-0001 (intake readiness workflow — its lifecycle stands; its
  implicit "the checker is the contract" model does not)
- **Related ADRs:** ADR-0001 (intake readiness workflow), ADR-0003 (intake state authority
  and label reconciliation — its single-recognition principle is generalized here)

---

## Context

TheForge validates issue bodies at several gates: `forge shape`, `forge groom`, the
ambient shape-check Action, and sprint admission. There is no shared specification of what
a well-formed issue is. Each gate reads the same `shape_check` module, and the module's
implementation *is* the contract — there is no artifact that states it independently.

Three consequences have been observed in the corpus rather than predicted.

**The system returns two readiness answers for one document.** `shape_check.check()`
produces a list of `Reason` objects carrying a severity. Two functions then reduce that
list, and they disagree by construction. `_mapping.map_shape` filters by severity — only
`Severity.BLOCKING` reasons downgrade a document, so an advisory yields
`Shape.RUNNABLE`. `verdict.derive_verdict` maps by code through a hand-ordered
precedence table that ignores severity, so the advisory code `no_observable_done_state`
yields `ShapeVerdict.NEEDS_GROOMING_MISSING_AC`. Issue #2230 evaluates as
`shape: runnable` and `verdict: needs_grooming_missing_ac` simultaneously. Sprint
admission consumes the first; `forge shape` and `forge groom` report the second. An
operator reading "needs grooming" and a scheduler reading "runnable" are both reading the
system correctly.

**Documentation of the contract cannot stay true.** The rules exist once, in the checker,
and every prose restatement has drifted from it. `docs/guides/authoring.md` names six of
the thirty enforced acceptance-criterion verbs, inside a section about refactors, and
states neither that the list is closed nor that it is tense-sensitive — `writes` satisfies
it and `written` does not. `docs/reference/bug-shape.md` covers the Diagnosis bullets and
nothing else. The `implementation_plan_in_body` rule appears only in ADR-0003 and the
changelog. No command emits the rules; an author discovers each one by submitting a body
and reading the refusal.

**Producers emit bodies their own consumers refuse.** The post-run hook files findings
with no type label, so the classifier evaluates defect reports as features and demands
acceptance criteria from them (#2713). `forge diagnose` appends a Diagnosis section whose
`file:line` citations trip `implementation_plan_in_body` on non-bug issues (#2136).
`forge groom`, run on an untyped finding, scaffolds the feature sections the classifier
asked for, and those sections then contradict the type label when one is added.

These are not separate defects. They are what follows from having no contract: nothing to
derive a single answer from, nothing to render documentation from, and nothing for a
producer to render against.

## Decision

**1. A declarative typed specification is the contract.** A per-type `IssueShapeSpec`
declares canonical type label, section headings and their order, which sections are
required and which are forbidden, field-level constraints, and lifecycle states. The
specification is data. The checker validates against it, documentation is rendered from
it, and producers render through it. Markdown is a rendering of the structure, never the
structure itself.

**2. Evaluation returns four separable results, not one enum.** Compressing distinct
questions into a single readiness value is what produced the two-answer defect. A result
carries:

- **Structural status** — does the document satisfy the typed grammar?
- **Admission** — may it enter implementation?
- **Routing recommendation** — what should happen to it next?
- **Advisories** — non-blocking observations that inform a reader and decide nothing.

**3. One admission answer, consumed everywhere.** `forge shape`, `forge groom`, the ready
queue, the ambient relabeler, and sprint admission consume the same admission result. No
surface derives its own. This generalizes ADR-0003's principle — a body admitted by one
gate must not be refused by another — from heading recognition to the whole evaluation.

**4. Advisories never decide admission.** An advisory is an observation. Where a condition
must block, it is a structural rule or an explicit lifecycle rule and says so.
`diagnosis_cause_unknown` is non-admissible because the lifecycle requires a confirmed
cause before implementation, not because an advisory code won a position in a precedence
table.

**5. Syntax and judgment are separated.** Structural validation answers only mechanical
questions: is the type declared, are required sections present, are forbidden sections
absent, do declared relationships resolve. Whether an acceptance criterion is meaningful,
sufficiently precise, or expresses the author's intent is a semantic question and does not
belong in the structural gate. Presence of acceptance-criterion bullets is structural;
whether they are observable is semantic. The closed verb list is retired as an admission
input.

**6. Semantic review is a distinct, explicitly probabilistic stage.** It may not alter
structural validity, and it may not be implemented by adding heuristics to the structural
gate. Its findings identify the model, prompt, and version that produced them.
`STRUCTURALLY_VALID` is a factual claim about grammar; `REVIEWED_READY` means a recorded
semantic review was passed, not that the document is mechanically proven sound.

Raw semantic-review findings never change admission. Where policy requires semantic review
before implementation, admission consumes a recorded, operator-ratified review state — never
model output directly. Without this the stage becomes a second admission gate whose verdicts
are probabilistic and unappealable, which is the failure this ADR exists to prevent, rebuilt
one layer up.

**7. Every producer renders and pre-validates.** Any component that writes an issue body —
`shape`, `groom`, `diagnose`, `report`, the post-run hook — renders through the
specification and validates the result before mutating anything. A producer that cannot
produce a conforming body fails loudly rather than filing an object that is dead on
arrival. Producer conformance is covered by tests, not by convention.

**8. Parsing, rendering and round-tripping are part of the contract.** A specification that
governs only emission leaves the larger surface — the existing corpus, and every body an
operator writes by hand — outside it. The contract therefore covers the whole cycle:

- Canonical and legacy Markdown both parse into a typed `IssueDocument`; recognition of a
  legacy spelling on input never makes it canonical on output.
- Renderers emit only the canonical form.
- `render(parse(canonical_body))` is idempotent — a conforming document survives a
  round trip unchanged.
- Prose the specification does not model is preserved, not discarded. An operator's
  reasoning, worked examples and asides are the reason issues are readable, and a
  normalizer that drops what it cannot classify destroys the input it was meant to serve.
- A conforming document is not rewritten because a producer touched it. Producers normalize
  what does not conform and leave what does alone.

This is where the failure mode has already been observed: #2053, where `forge shape --apply`
rewrote a gate-passing bug body into one the gate refused. A renderer that does not
round-trip is a destructive edit with a schema attached.

## Consequences

**A canonical spelling must be chosen.** ADR-0003's single-recognition principle led
`shape_check` to accept both `What happened` / `What was expected` and `Observed` /
`Expected` (#2139), correctly, so that one gate could not refuse what another admitted.
But nothing declared which is canonical, so `intake/shape_render.py` emits `Observed`
while `docs/guides/authoring.md` mandates `What happened`. Compatibility aliases became an
accidental contract. Recognition of both continues; the specification names one canonical
form, and renderers emit that form.

**Documentation becomes generated.** Prose that restates the rules is replaced by
rendering from the specification, verified in CI. Guides continue to carry worked examples
and rationale, which are not derivable.

**The verdict precedence table is retired.** It presently carries routing decisions rather
than readiness — `implementation_plan_in_body` maps to `ADR_CANDIDATE`, and
`bug_fix_location_prescription` maps to `RUNNABLE`. Those are routing recommendations, and
they move to the routing result rather than being compressed into a readiness enum.

**Existing intake work is re-scoped, not discarded.** #2510's canonicalization work stands;
its premise that #2139 loosened the contract to match the corpus does not, and is corrected
here. #2408 is re-framed as a front end that compiles typed fields into a canonical
rendering, and its present criterion — that the authoring path name the vocabulary that
counts as observable — is withdrawn, because it would make the closed verb list permanent.

**A conforming document may still be wrong.** #2408 satisfies the structural gate today
while encoding the defect above. That is the clearest available evidence for clause 5: a
structural gate can establish that an object conforms to the contract, and cannot
establish that the contract it conforms to is correct. Only semantic review reaches that,
and only probabilistically.

## As built (slice 3, #2725)

The specification, the typed `IssueDocument`, and the parse/render halves landed
in `theforge.shape_check.issue_spec` and `theforge.shape_check.document`. The
checker derives its recognized type labels, required/forbidden sections,
section-recognition spellings and lifecycle refusals from that data, and
`docs/reference/issue-shape.md` is generated from it and checked in CI. The
closed observable-verb vocabulary is retired as an admission input; the
`no_observable_done_state` code remains for the structural branches (no
acceptance-criteria section, or one with no bullets).

Two decisions of this ADR are deliberately **not** in that slice, and are
recorded here so the deferral is intentional rather than forgotten:

- **Clause 2 (four separable results).** `ShapeResult` keeps its current
  surface — `shape`, `verdict`, `admits_implementation_sprint` — because no
  criterion of that slice needed the richer result and migrating every
  admission consumer alongside a new contract would have coupled two changes
  that can fail independently. The compatibility invariant holds meanwhile:
  admission is derived from the verdict, and the verdict from the
  specification's structural and lifecycle outcome.
- **Clause 7 (every producer renders through the specification).** `forge
  shape`'s body restructure does; `diagnose`, `report`, the post-run hook, and
  advisory filing still assemble Markdown directly. Those are the remaining
  producer migrations.

**The canonical bug-section spelling is `Observed` / `Expected`.** The corpus
and `forge shape` already wrote them; `What happened` / `What was expected`
remain recognized and are rewritten to the canonical form on output.

## As built (slice 6, #2685 + #2785)

Clause 6 is now implemented, in two halves that were deliberately separated so
the policy could be decided against measurements rather than ahead of them.
#2685 built the evaluator as an audit-only instrument that changes nothing.
#2785 decided what its output is allowed to mean and wired that decision:

- **Readiness is a derived state, not a stored one.** `REVIEWED_READY` is
  computed per document revision in `theforge.eval.semantic_readiness` from two
  separately recorded facts — an evaluation record and an operator ratification —
  both scoped to the same `input_digest`. Nothing writes a readiness flag, so
  there is no state to go stale unnoticed; a changed revision simply has no
  record and reports `unevaluated`.
- **A clean evaluation is not readiness.** Readiness follows an operator
  ratification (`forge ratify-semantic`) that decides every concern raised.
  Rejecting a concern clears it for that revision; accepting one withholds
  readiness for that revision until the document changes.
- **Applicability and evaluation state are separate axes**, per clause 2. Policy
  requires a ratified review for `bug`, `enhancement`, `task` and `spike` in the
  `implementation_ready` lifecycle state, and for nothing else; a document policy
  does not name still reports its evaluation state truthfully and keeps its
  existing structural/lifecycle admission result.
- **Unratified concerns are not a refusal.** `unevaluated` and
  `awaiting_ratification` withhold admission under the same reason code —
  `semantic_review_not_ratified` — because the withholding fact in both cases is
  the absence of ratified readiness, not the content of any finding. This is the
  concrete guard against clause 6's failure mode: admission never reads a
  finding as a verdict.
- **The structural verdict is untouched.** The overlay is consulted only after
  `classify_admissibility` admits, it emits no `ShapeVerdict`, and a semantic
  withholding carries semantic reason codes with no verdict claim. `--force`
  overrides shape refusals and does not override semantic readiness, on the same
  grounds as the `operator-action` label.

The policy is stated for readers in `docs/reference/semantic-readiness.md`.

## Alternatives considered

**Generate documentation from the checker.** Keeps the implementation as truth and makes
prose a derivative. Removes doc drift but freezes the current implementation as the
specification, including the closed verb list and the two-answer reduction. Rejected: it
solves the symptom that is cheapest to see.

**Fix the two reductions to agree.** Choosing `_mapping` or `verdict` as authoritative and
deleting the other. Rejected: both compress separable concerns into misleading enums, and
agreeing on one compression preserves the defect that produced the disagreement.

**Add the missing rules to the authoring guide.** The response to every previous instance,
and the reason there are now four partial restatements. Rejected: a fifth is a fifth thing
to drift.
