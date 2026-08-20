# Measurement: how often dev resolves an advisory plan-review finding

Status: record (2026-08-19, issue #2112). This is a measurement, not a policy
change. It replaces an instinct with a number; what to do about the number is a
separate decision this record does not pre-empt.

Reproduce with `forge audits plan-advisory` (add `--verbose` for each finding's
text and the evidence its judgment cites). The read model lives at
`src/theforge/plan_advisory/`; the judgment corpus is
`src/theforge/plan_advisory/judgments.json`.

## The question

Plan review approves a plan while holding unresolved P1-level findings and
passes them to dev as advisory context. The policy has never been evaluated
against outcomes. It turns on one unmeasured quantity: **how often does dev
resolve an advisory plan finding it was handed?**

## Result

```
Plan advisory resolution — 29 runs, 104 judged findings of 104 extracted (coverage 100%)
  audit records scanned: 254
  corpus: completed (DONE) runs carrying a plan review with P1-level plan findings
  excluded by final_phase: 8 run(s) carrying 29 P1-level finding(s) (reached dev but did not ship)
  P2 plan findings out of scope: 56
  P1-level findings raised: 119; 15 resolved by plan regeneration before dev (not advisory, excluded from the rate)
  unjudged findings excluded from every rate: 0
  judgments carrying no citable evidence: 0

overall: 100/104 advisory findings resolved (96%); 100 shipped-addressed, 4 not

class                        findings  resolved  escaped   rate
---------------------------------------------------------------
missing failure mode               25        23        2    92%
module/placement                   25        25        0   100%
unspecified mechanism              19        18        1    95%
scope / out-of-scope               16        15        1    94%
factual error in rationale         10        10        0   100%
contract/interface mismatch         5         5        0   100%
test strategy gap                   3         3        0   100%
already-implemented                 1         1        0   100%

plan review cost:  median $4.97/story (n=18)   median 24% of story cost (n=17)
  of 29 corpus run(s): 11 record no plan-review cost, 12 no story total — both omitted rather than imputed

escapes by later detection point:
  unshipped 2 · adopter run 1 · own later story 1

escaped findings (4):
  issue-2050   unspecified mechanism      not addressed  -> caught at adopter run
  issue-2111   missing failure mode       not addressed  -> caught at own later story
  issue-2284   scope / out-of-scope       not addressed  -> never caught (still latent)
  issue-2347   missing failure mode       not addressed  -> never caught (still latent)
```

## What the four escapes are

- **issue-2050 · unspecified mechanism · adopter run.** The plan defined a
  request/response channel but not how the agent blocks between writing a
  request and reading a response. `2d5bfdd1` shipped the protocol without the
  discipline. Rediscovered when an adopter story produced no work at all;
  shipped as #2077 at roughly $4.43 across the wasted run and two fix stories,
  plus two days during which the adopter could not use the capability.
- **issue-2111 · missing failure mode · own later story.** Ungating
  `_has_base_commit_referencing_issue` let a bare `#N` mention count as merge
  evidence. Caught by issue-2374, which needed `e82b1478` plus two further fix
  commits (`76cf65cc`, `2c67da01`) to narrow it.
- **issue-2284 · scope / out-of-scope · never caught.** `a96d96bc` changed what
  `dev_cost_estimate_usd` means (score-scoped history × `BAND_HEADROOM`) without
  touching the two consumers that read `state.adaptive_dev_cost_estimate_usd` as
  an overrun threshold (`dev_phase.py:1666,1676`, `review_phase.py:2326`). Still
  latent.
- **issue-2347 · missing failure mode · never caught.** `617e1014` added
  `CoordinatorState.changed_files` without registering it with
  `resume_persistence.py`, which serializes explicit blocks. A resumed run drops
  the capture silently. Still latent.

## What this does and does not establish

The rate is **96%**, and no class falls out of line the way the spec anticipated
`unspecified mechanism` might: its 95% sits inside the spread of every other
class with more than a handful of findings. On this corpus there is no class
whose escape rate justifies an extra plan round on its own.

The cost side sharpens that. Plan review is a **median 24% of story cost** —
several times the 3.7% the story's illustrative shape assumed. An additional
plan round is not a rounding error against what it guards; it is a quarter of
the story again, spent on 104 findings to catch some fraction of four.

Three qualifications, all of which the report renders rather than hides:

1. **Escapes are cheap to detect and expensive to miss, asymmetrically.** Two of
   four escapes were caught downstream, at real but bounded cost. The other two
   were never caught and are still latent — they were found *by this analysis*,
   not by any process. A 96% rate says nothing about the tail cost of the 4%.
2. **The cost denominator is thin.** 11 of 29 corpus runs record no plan-review
   cost and 12 no story total, so the 24% median rests on 17 runs. These are the
   cost-unmeasured runs (#2019, #2020 and successors); the omissions are counted
   rather than imputed.
3. **DONE-only selection is a real bias.** 8 further runs carried 29 P1-level
   plan findings and ended `ESCALATE`, `MERGE_FAILED` or `PLAN_REVIEW`. "Did the
   change that shipped address it" has no answer where nothing shipped, so they
   are excluded from the rate — but a plan finding escaping into a run that then
   escalated is exactly the expensive shape, and it is not represented here.

Two facts from the substrate also correct the story's stated census, which was
written against nine runs and is stale:

- Plan **regeneration does happen**: 15 of 119 P1-level findings (12.6%) were
  resolved by a regenerated plan before dev saw them, across runs including
  issue-2335 and issue-2347. The policy is not "never regenerate".
- The corpus is 29 runs and 119 P1-level findings, not 9 and 11.

## Choosing among the three options

- **Keep advisory-only.** Supported by everything above: a 96% resolution rate
  against a plan phase already costing a median 24% of the story. This is the
  option the numbers favour.
- **Amend plans for selected finding classes.** Not supported. The class table
  gives no candidate — `unspecified mechanism` is 95%, and the two classes
  carrying escapes (`missing failure mode` at 92%, `scope / out-of-scope` at
  94%) are the two largest and most heterogeneous, so gating on them means
  gating on most findings.
- **Carry unresolved plan findings forward into review.** The option the escapes
  actually point at, and the one this measurement cannot decide. All four
  escapes are of a shape code review is positioned to catch — a consumer not
  updated, a state field not persisted, a channel with no blocking discipline —
  and two of them were never caught by anything. Unlike an extra plan round, it
  costs no additional agent invocation: the findings already exist and review
  already runs. Deciding it needs a measurement this record does not make: how
  often review would have caught them if it had been handed them.

## Corpus construction and exclusions

- **Mechanical half** (read-only, from the audit substrate via `open_readonly`):
  which P1-level findings each run carried, their registry dispositions, and
  `plan_review.cost_usd` against `cost.total_usd`.
- **Judged half** (`judgments.json`): finding class and whether the change that
  shipped addressed it. No audit field records either —
  `plan_finding_registry` entries carry only `description`, `severity`,
  `original_severity`, `effective_severity`, `cycle_first_seen`,
  `cycle_last_seen` and an in-run `disposition`, none of which speak to the
  shipped diff. Every row cites a commit, a code site, or the run's own dev
  handoff notes; the report counts rows marked `evidence unavailable` separately
  (currently 0).
- **Findings are keyed** by `run_id:ordinal:sha256(normalized description)[:8]`,
  so a re-ordered registry or a repeated description in one run both survive.
- **Coverage is validated in one direction.** A judgment naming no carried
  finding is a hard error. An audit finding with no judgment is not: it is
  counted as unjudged, rendered, and excluded from every rate denominator, so
  the substrate can grow past the corpus without the rate silently narrowing.
- **Excluded:** P2 plan findings (56); P1-level findings resolved by plan
  regeneration before dev (15); runs that did not reach `DONE` (8 runs, 29
  findings, enumerated in the report).
- **Not available:** the `hdp` adopter's three plan-review runs. That repository
  is not reachable from this workspace, so its records are in no count here.
