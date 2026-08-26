# ADR-0010: Backlog Triage Disposition Shelved; Semantic Verification Retained

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** v0.16.0
- **Supersedes in part:** the disposition-automation premise of #717 and epic #1033. The
  five delivered slices (#2227–#2231) stand as code; the product goal they served does not.
- **Related ADRs:** ADR-0002 (audit substrate — the evidence-provenance model retained
  here), ADR-0006 (adaptive router trust boundary — the sealed-agent pattern retained here)

---

## Context

`forge triage` was scoped in #717 to choose `fix_now`, `fix_later`, or `punt` for open
findings, and decomposed in #1033 into five sequential slices: a deterministic backlog
report, an agent proposal, an adversarial punt review, per-finding operator ratification,
and headless persistence. All five landed.

In the one operator-reported pass on record, over the backlog at v0.16.0rc1, it spent
$1.6272 across 30 findings and returned 29 `needs_verification`, 1 `fix_later`, and 0
punts — therefore 0 adversarial reviews, since a proposed punt is that stage's only
trigger. Those figures are as reported by the operator running that pass; no triage
proposal run is recorded in the audit substrate, so they cannot be re-derived from it, and
there is no before/after series across the 2026-08 citation-resolution fixes (#2690, #2692,
#2711, #2712, #2738, #2739, #2691, #2693). The argument below does not rest on them: it
rests on what the evidence model can express, which is a static property of the code.

Three findings, measured rather than predicted, explain why.

**The evidence vocabulary cannot answer the question asked of it.** Every `EvidenceEntry`
the report can produce is path presence, symbol presence, line-in-range, or churn count
(`src/theforge/triage_backlog_report.py`). The proposer's packet is its entire world by
design — `src/theforge/task/triage_prompts.py` forbids investigation, and
`src/theforge/coordinator/triage_proposal_flow.py` runs it in an empty scratch directory so
that the constraint is mechanical rather than prompt-side. The packet labels the finding
body "the claim as filed — NOT evidence that it still holds," and then supplies no evidence
bearing on whether it holds. `needs_verification` is the specified, correct output. Refusal
is working; it refuses because the question is unanswerable from the inputs.

**Better citation resolution reduces disposition yield.** The only positive staleness
signal in the vocabulary is `cited symbol absent from current tree`. The 2026-08 fixes
converted spurious `absent` results into `present at file:line`, and presence licenses no
disposition. Correctness improved and yield fell. Re-running triage on a new rc cannot
validate the feature, because the evidence vocabulary is unchanged.

**Presence evidence is unreliable, and sometimes inverted.** On #1212 the symbol search
located `iterations` at `src/theforge/coordinator/audit.py:345` — the plan-review field —
while the finding concerns `len(state.dev_results)` at `audit.py:361`, and reported the hit
as corroboration. On #1157 the evidence reads `cited symbol TimeoutExpired absent from
current tree`: true, and the absence *is the defect* — `sprint/runner.py:1413` and `:1418`
still call `check_output` with no `timeout=`. On #659 the evidence reads `cited symbol
reviewer_demoted absent`, which is false; it is at `coordinator/state.py:648`.

**The stale findings the punt path serves were scarce in the sample examined.** Of nine
open findings hand-verified against `b9fbfaea`, one was stale (#1312). That cohort was
hand-picked for citation-shape spread, not sampled, so it establishes neither a prevalence
figure for the backlog nor that the population is absent. It is one observation, recorded
because the feature's value depends on that population being large and nothing in the
corpus yet shows that it is.

### The benchmark

An eight-finding benchmark against pinned baseline `b9fbfaea` (v0.16.0rc3) tested whether
semantic investigation could decide what presence-checking could not, under a scorecard
declared before any run. Cohort: #1212, #149, #1104, #2660, #1044 (active, chosen for
citation-shape spread), #1312 (stale), #1157 and #659 (active, and adversarial — their
presence evidence is inverted or false).

Two scores, and they differ. **Semantic judgment: 8/8 correct.** **Structured-record score
against the predeclared mapping: 7/8**, because #1312 serialized `already_resolved: false`
while its own narrative concluded the premise was removed — a machine-readable false-active,
and the defect recorded as #2760. Both numbers belong in any citation of this benchmark;
the narrative score alone overstates what a consumer of the artifacts would get.

| id | hand truth | narrative verdict | `already_resolved` | cost | duration |
|---|---|---|---|---|---|
| 1212 | active | active | false ✓ | $0.648 | 86s |
| 149 | active | active | false ✓ | $0.579 | 94s |
| 1104 | active | active | false ✓ | $0.521 | 83s |
| 2660 | active | active | false ✓ | $0.446 | 63s |
| 1044 | active | active | false ✓ | $0.354 | 52s |
| 1312 | **resolved** | **resolved** (names removing commit `ed14ddde`) | **false ✗** | $0.600 | 180s |
| 1157 | active | active | false ✓ | $0.510 | 58s |
| 659 | active | active | false ✓ | $0.286 | 45s |

$3.94 total, ~$0.52 per finding. Zero false `resolved` in either score. Deterministic
premise-checking failed on 9 anchors across the first five findings — every finding produced
at least one — and the semantic verdict was correct regardless. On #149 the cited file
(`src/theforge/coordinator.py`) no longer exists at any path; the verdict was still correct.

The capability demonstrated is locating the correct cause and implementation seam on active
findings, which is distinct from confirming that a finding is active. #2660 is the clearest
case. Its own evidence line cites `sprint/runner.py:456`, where `shape_verdict` is taken as
`codes[0]`. A fix written against that line cannot work: `SkippedIssue.verdict` exists at
`sprint/shape_gate.py:91`, but the `audit={...}` dict built in
`sprint/entry_intake.py:94-99` never copies it, so no typed verdict is available at
`runner.py:456` to prefer. The seam is upstream of the cited line. Presence-checking cannot
reach that conclusion in principle — it can only confirm that `codes[0]` is still there.

This is not backlog reduction. It is grooming: turning an open finding into a runnable one
with the correct scope.

## Decision

1. **Disposition automation in `forge triage` is shelved.** No further investment in the
   deterministic report's evidence vocabulary, its citation resolution, the proposer packet,
   or the punt-review stage. `post_sprint_triage` remains `False` by default
   (`src/theforge/config/types.py:1226`), and this repository sets no `triage` key.

2. **The code is preserved, not deleted.** The report, audit, punt-review, ratification, and
   headless-persistence modules stay in tree. They encode working mechanics — sealed agents,
   verbatim-quote grounding, per-finding operator ratification, pending-decision persistence
   — that a future disposition product would otherwise rebuild.

3. **The shelf is targeted at the spending paths, and is enforced at dispatch, not at
   flags.** `forge triage` selects its mode on TTY presence as well as flags
   (`src/theforge/cli/triage.py`), so a flag-keyed guard would shelve the one free mode and
   leave every spending path open. The complete matrix:

   | invocation | dispatch | spends | shelved |
   |---|---|---|---|
   | no flag, interactive | `_cmd_triage_report` | no | **no — preserved** |
   | no flag, headless | `_cmd_triage_headless` | yes | yes |
   | `--report PATH`, interactive | `_cmd_triage_proposals` | yes | yes |
   | `--report PATH`, headless | `_cmd_triage_headless` | yes | yes |
   | `--ratify ID` | `_cmd_triage_ratify` | no | **no — preserved** |
   | `--discard ID` | `_cmd_triage_discard` | no | **no — preserved** |

   The direct post-sprint entry `sprint/post_sprint_triage.py:run_post_sprint_triage` →
   `coordinator/triage_headless_flow.py:run_headless_triage` is shelved on the same terms;
   the config default already disables it, and the shelf must not depend on that default
   remaining unchanged. A defensive guard immediately before proposer dispatch covers any
   path not enumerated here — the matrix above was derived incorrectly once during this
   decision, which is the reason the guard is required rather than optional.

   Report generation, `--ratify` and `--discard` remain available. Shelving them would
   strand any persisted pending decision with no way to inspect, apply, or drop it.

4. **The shelf is visible at the point of use.** `forge triage` states which modes are
   shelved and cites this ADR, and the CLI help distinguishes "report supported" from
   "disposition proposals shelved." An operator who types the command learns the decision
   from the command, rather than from a document they would have to already know to read.
   This follows the project's standing preference for mechanical over prompt-side
   constraint.

5. **`forge diagnose` is the surviving capability for finding verification.** It already
   accepts `--issue a,b,c` with `--parallel N` and `--dry-run`, stamps a baseline SHA,
   records inspected-file provenance, and distinguishes a confirmed cause from
   `ALREADY_RESOLVED` from honest refusal. Bulk semantic verification requires no new
   subsystem; it requires that this one's structured output be trustworthy. Defects in those
   structured records are therefore load-bearing as of this ADR: #2760 (a diagnosis
   concluding the premise was removed serializes as `already_resolved: false`, and an
   honest refusal serializes identically, so the two are indistinguishable to a consumer)
   and #2678 (the fallback `UncheckedPremise` places free-text prose in a field consumed as
   a repository path). Both are v0.16.0 and proceed through the normal pipeline.

6. **Disposition and prioritization have no automated tool, and this is a standing
   condition, not a gap awaiting a fix.** Deciding fix-now versus fix-later versus another
   milestone versus close is an operator judgment today. It is not derivable from severity
   and milestone: this backlog is uniformly `p2` across two milestones, so any such policy
   returns a near-constant label. `forge diagnose` supplies better evidence for these
   choices and does not make them. A future attempt requires impact, effort, milestone
   capacity, and release goals, must be benchmarked on its own terms before implementation,
   and should not reuse the name `triage`.

## Consequences

**The finding backlog is not automatically dispositioned, and will not be.** It is groomed
by `forge diagnose` producing verified active/resolved artifacts with the correct seam, and
by the operator deciding what to run. The operator map recording which command answers which
question lives in `docs/guides/controller-runbook.md`, including the row that says no tool
answers the disposition question.

**Bulk verification costs about 10x the shelved feature.** ~$0.52 per finding against
$1.6272 for a 30-finding triage pass — roughly $17 for a 32-finding backlog. The comparison
flatters triage only because its output is unusable; the honest framing is $17 for verified
seams against $1.63 for 29 refusals.

**The process failure is the transferable lesson.** Every slice of #1033 passed its local
acceptance criteria, and no slice owned an end-to-end utility criterion — whether the
assembled pipeline could decide anything. The proposal stage was also implemented and
landed before the deterministic report it consumes, so it was validated against synthetic
packets rather than the real evidence producer. Both stories in #2227 and #2228 used
"finding text still matches current code" in their examples while no specified stage
gathered current code content; the gap was visible in the stories and no reviewer owned the
question. Slice-level review cannot catch this, because the defect is in the composition.

**The proposer sandbox pattern is validated and reusable.** The empty scratch directory and
verbatim-quote grounding are the right shape for any advisory agent that must not mutate.
Clause 2 preserves them deliberately.

**Nothing here weakens refusal doctrine.** Triage's 29 `needs_verification` were correct
refusals. The lesson is not that the system refused too readily; it is that a stage was
built to answer a question its inputs could not reach, and refused honestly every time. See
`docs/vision/refusal-capability.md`.

## Alternatives considered

**Reshape triage around a sealed semantic verifier.** A verifier with a read-only repository
view produces a provenance-rich packet; the sealed proposer consumes it and proposes a
disposition. Faithful to the original architecture and the most obvious reading of the
benchmark. Rejected: it preserves a proposer stage whose justification was that judging
staleness needs fresh eyes, and staleness was one of nine findings in the sample examined. Once the verifier
returns an active finding with a seam and quoted evidence, the proposer adds a label, not a
judgment.

**Collapse: drop the proposer, assign dispositions by deterministic policy from milestone
and severity.** Cheaper, fewer stages, reuses the surrounding machinery. Rejected on the
data: every finding in the backlog is `p2`, in either Hygiene or v0.16.0, so the policy
output is `active → fix_later → Hygiene` for nearly all of it. That is a relabel, and the
inputs the decision actually needs are precisely the ones not recorded.

**Continue citation-resolution work.** The response to each of the previous four rounds.
Rejected: measured directly. Premise-checking failed on 9 of the first cohort's anchors and
changed no verdict, and improved resolution has been shown to *reduce* punt yield.

**Delete the triage subsystem.** Removes shelved code and the risk of an operator trusting
it. Rejected: clauses 3 and 4 address the trust risk at a fraction of the cost, and the
sealed-agent and ratification mechanics are worth more in tree than the lines are worth
removed.
