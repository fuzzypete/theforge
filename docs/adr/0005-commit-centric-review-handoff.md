# ADR-0005: Commit-Centric Review Handoff

- **Status:** Accepted
- **Date:** 2026-07-20
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** all (review phase is on the sprint hot path today)
- **Related issues:** #1534 (v0.12 capture), split item 1; #1825 (this promotion)

---

## Context

TheForge's review phase decides whether a dev agent's work is done. Two ways to
frame that decision have circulated informally in project memory and in
`CONVENTIONS.md`:

1. **Commit-centric:** the dev agent gets the spec and implements freely; the
   commits it produces are the handoff artifact; the reviewer evaluates those
   commits against the spec and the project's structure/conventions. This is
   the HDP-style review model TheForge has been converging on since
   `project_hdp_vision.md`.
2. **File-scope / diff-stat:** the reviewer (or a pre-review step) first
   derives an expected file scope or a diff-stat summary — which files
   *should* have changed, how many lines — and evaluates the dev agent's work
   against that derived scope instead of against the spec directly.

The second framing looks like a reasonable optimization: it gives the
reviewer a smaller surface to check and a cheap way to flag "unexpected"
changes. In practice it inverts the trust relationship the coordinator is
supposed to enforce. A pre-groomed file scope is itself a guess about
implementation shape, produced before the dev agent has done any work. When
review evaluates against that guess instead of against the spec:

- Legitimate implementation choices that touch files outside the guessed
  scope get flagged as deviations, even when they are the correct fix.
- A dev agent that mechanically matches the guessed scope while missing the
  actual spec intent looks "clean" to a diff-stat check and passes review it
  should not.
- The reviewer's attention shifts from "does this satisfy the spec" to "does
  this match the plan," which quietly reintroduces the coordinator making an
  LLM-flavored routing decision about implementation shape — the thing
  `CONVENTIONS.md` rule 1 ("Coordinator is pure Python — no LLM calls for
  routing or process decisions") and the audit-substrate trust model
  (ADR-0002) both work to keep out of the mechanical path.

This has surfaced repeatedly as review-cycle churn: reviewers building or
consuming replacement metadata (expected-file lists, diff-stat summaries)
that compensate for missing commit context instead of just reading the
commits. `project_churn_root_cause.md` traces one concrete instance of this
pattern to ground; this ADR generalizes the fix so it doesn't have to be
rediscovered per incident.

`CONVENTIONS.md`'s existing "Review should stay commit-centric and PR-shaped"
section states the direction but does not name the failure mode it rules out
or connect it to the substrate-trust model. This ADR is that missing
artifact: it exists so the principle survives as a citable decision instead
of living only in operator memory and scattered conversation.

## Decision

> **Review handoffs are commit-based — like PRs without GitHub. The dev
> agent gets the spec and implements freely. The commits it produces are the
> primary handoff artifact. The reviewer evaluates those commits against the
> spec and the project's structure and conventions — never against a
> pre-groomed file scope or a diff-stat summary.**

Consequences of the headline principle:

- **No pre-groomed file scope.** Nothing upstream of review — preflight,
  planning, or the coordinator — may hand the reviewer a "files that should
  change" list to check the dev agent's work against. `likely_files` and
  similar preflight hints are orientation for the dev agent, not a review
  contract.
- **No diff-stat gating.** Line counts, file counts, or "unexpected files
  touched" are not review signals on their own. A commit that touches more
  or fewer files than expected is neither evidence for nor against
  correctness; only spec conformance is.
- **Commits are read, not summarized-around.** If a reviewer needs
  replacement metadata (an expected-scope list, a diff-stat digest) to make
  a call, that is a signal the commit context itself is insufficient —
  fix the audit trail (per ADR-0002's authoritative-record contract) rather
  than building a workaround artifact.
- **The audit trail lives in the repo.** Per ADR-0002, per-run records with
  `provenance='native'` are the authoritative source of what happened;
  review reads commits and those records, not a GitHub-hosted PR diff view
  TheForge does not depend on.

## Consequences

- Review-phase prompts and tooling must present commits (and their
  diffs) as the primary review surface, not a derived scope or stat summary.
- Preflight/planning outputs that suggest likely files remain useful as dev
  orientation but must not be threaded into review as a checklist.
- This does not forbid a reviewer from *noticing* an unusually large or
  unusually scoped diff and asking a question about it — it forbids treating
  scope match/mismatch as a pass/fail signal in place of spec conformance.

## Out of scope

- This ADR does not change how commits are produced, batched, or squashed by
  dev agents — only how review evaluates them.
- It does not address gate/CI mechanics (`make gate`), which remain a
  separate, mechanical pass/fail signal alongside review.

## References

- `CONVENTIONS.md`, "Review should stay commit-centric and PR-shaped"
  (pre-existing statement of direction; this ADR supplies the failure mode
  and rationale that section referenced but did not spell out)
- ADR-0002: Audit Substrate and Queryable Run History (`docs/adr/0002-audit-substrate-and-queryable-run-history.md`)
  — trust-boundary contract for what review may treat as authoritative
- `project_hdp_vision.md`, `project_commit_centric_review.md`,
  `project_churn_root_cause.md` (prior memory entries this ADR promotes and
  narrows)
