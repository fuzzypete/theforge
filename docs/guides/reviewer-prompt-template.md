# Reviewer Prompt Template (Verification-Gated)

A reusable prompt template for driving an LLM code reviewer that **grounds its
review in the actual source of truth before analysing anything**. Copy the
template below verbatim into a reviewer session (or a reviewer-pool profile
prompt) and fill in the bracketed slots.

This is the review-trust counterpart to the intake-readiness gate described in
the [refusal-capability doctrine](../vision/refusal-capability.md): the doctrine
names review as a boundary where the system must be able to refuse rather than
confabulate, and this template is the concrete artifact that makes refusal the
cheap path. It directly addresses **reviewer tree-currency** — a reviewer that
critiques a stale checkout produces confident, well-written findings about code
that no longer exists.

## Why this exists

During the 2026-05-10 ADR review, an ungated reviewer critiqued a **stale
checkout**, rationalised the mismatch instead of aborting, and produced fluent,
authoritative-sounding findings about code that was not the code under review.
The failure mode is **confabulation**: an LLM will happily narrate a plausible
review of whatever it *thinks* it is looking at, and prose fluency is no signal
of grounding.

Re-running the same review with the verification gates below converted that
reviewer into one that first *proved* it was looking at the right tree, then
tagged every claim with a certainty level, then refused to flatter. The output
became verifiable and citation-grounded.

The template encodes four gates, in order:

1. **Tree-state proof, before analysis** — the reviewer must first quote git
   metadata and per-file evidence proving it is looking at the code under
   review. This comes *first* on purpose: it makes confabulation more expensive
   than admitting ignorance. If the reviewer cannot produce the proof, it must
   abort, not improvise.
2. **Per-file quote checklist** — for each file it claims to have reviewed, the
   reviewer must quote a real line with its line number. A claim about a file it
   cannot quote is not a finding.
3. **Per-claim certainty tags** — every claim carries `[VERIFIED]`,
   `[INFERRED]`, or `[SPECULATIVE]`, so the reader can separate "I saw this" from
   "I'm guessing".
4. **Anti-flattery framing** — the reviewer is told that agreement is not the
   goal, that "looks good" with no evidence is a non-answer, and that a
   well-reasoned "I could not verify this" outranks a confident guess.

## The certainty tags

Every claim in the review — every finding, every compliance statement, every
"this looks correct" — must be prefixed with exactly one tag:

- **`[VERIFIED]`** — I read the exact line(s) and can quote them. Reserved for
  claims backed by a direct quote with a file path and line number. This is the
  only tag that may support a blocking finding.
- **`[INFERRED]`** — I did not read the exact line, but I am reasoning from
  something I did read (a caller, a type signature, a test). State the
  inference chain. Inferences may motivate a question, not a blocking verdict.
- **`[SPECULATIVE]`** — I am guessing from patterns or priors, not from this
  codebase. Speculative claims are prompts for the reader to check, never
  findings on their own.

The ordering matters: **a `[SPECULATIVE]` guess must never be laundered into a
`[VERIFIED]` finding.** If you cannot quote it, you cannot verify it.

> These per-claim tags are deliberately distinct from the automated dev-cycle
> pipeline's AC-level `VERIFIED / PARTIAL / NOT_VERIFIED` scheme
> (`src/theforge/task/review_prompts.py`). That scheme answers "is this
> acceptance criterion satisfied?"; these tags answer "how do I know this
> individual claim is true?" Use this template for operator-driven or ad-hoc
> reviews (ADR reviews, spot-checks, cross-reviews) where no schema validator is
> enforcing grounding for you.

## The template

Copy everything in the block below into the reviewer session. Replace the
bracketed slots (`[...]`) with your specifics.

```text
You are reviewing [WHAT IS UNDER REVIEW — e.g. "the diff on branch
feat/x against main", "ADR-0007", "the changes in PR #123"].

Your job is to determine whether this change is safe to accept. You are NOT
implementing anything and you are NOT here to be agreeable. A well-reasoned
"I could not verify this" is worth more than a confident guess. "Looks good"
with no cited evidence is a non-answer and will be treated as if you said
nothing. Do not soften findings to be polite; do not invent findings to seem
thorough.

=== GATE 1: PROVE YOU ARE LOOKING AT THE RIGHT TREE (do this FIRST) ===

Before you analyse anything, prove you are grounded in the actual source of
truth. You MUST complete this section before writing a single finding. If you
cannot complete it, STOP and report that you cannot ground the review — do not
proceed on assumptions.

Paste the real output of:
  - `git rev-parse HEAD`            (the commit you are actually reviewing)
  - `git status --short`            (prove the tree is clean / know what is dirty)
  - `git log --oneline -5`          (prove the history matches what you expect)

Confirm this matches [THE EXPECTED HEAD / BRANCH / ARTIFACT]. If it does not
match, STOP: you are reviewing the wrong tree. Say so and abort.

=== GATE 2: PER-FILE QUOTE CHECKLIST ===

For each file you will make any claim about, quote one real line WITH its line
number, copied from the file as it exists at the HEAD you proved above:

  - path/to/file.py:NN  |  <verbatim line>

A file you cannot quote is a file you have not read. You may not make a
[VERIFIED] claim about a file that is not on this checklist.

=== GATE 3: TAG EVERY CLAIM ===

Prefix EVERY claim with exactly one certainty tag:

  [VERIFIED]    I read the exact line(s) and quote them (path:line). Only
                [VERIFIED] claims may support a blocking finding.
  [INFERRED]    I reason from something I did read (a caller, a signature, a
                test). State the inference chain. Motivates a question, not a
                block.
  [SPECULATIVE] I am guessing from patterns/priors, not from this codebase. A
                prompt for the reader to check, never a finding on its own.

Never promote a [SPECULATIVE] guess into a [VERIFIED] finding. If you cannot
quote it, you cannot verify it.

=== NOW REVIEW ===

Only after Gates 1–3 are complete, evaluate the change:
  - Does it do what it claims / satisfy its acceptance criteria?
  - Correctness, data-integrity, and failure-mode risks.
  - Test coverage of the specific behaviour that matters.

For each finding: certainty tag, file:line quote, what is wrong, why it
matters. If you found nothing blocking, say so plainly and cite the
[VERIFIED] evidence that led you there — not a bare "looks good".
```

## Adapting it

- **Reviewer-pool profile.** Drop the template into a profile's prompt for
  operator-run pools. The automated sprint pipeline already enforces its own
  tree-state grounding (the "Verified Git Metadata" block that the coordinator
  injects from real git state) and AC-level verification; this template is for
  the reviews that pipeline does not cover.
- **Non-code artifacts.** For ADR or design-doc reviews, keep Gate 1 (prove you
  are reading the current revision of the document) and Gate 3 (certainty tags),
  and adapt Gate 2 to quote the sections you are evaluating.
- **Keep Gate 1 first.** The single most important property is that grounding
  precedes analysis. Moving the proof to the end lets the reviewer confabulate a
  review and then back-fill a plausible-looking justification.

## Related

- [Refusal-capability doctrine](../vision/refusal-capability.md) — review is a
  refusal boundary; this template is the review-trust artifact that keeps a
  reviewer from confabulating instead of refusing.
- [`src/theforge/task/review_prompts.py`](../../src/theforge/task/review_prompts.py)
  — the automated dev-cycle review prompt, with its own tree-state grounding and
  AC-level `VERIFIED / PARTIAL / NOT_VERIFIED` scheme.
- [Authoring guide](authoring.md) — how to shape the specs reviewers verify
  against.
