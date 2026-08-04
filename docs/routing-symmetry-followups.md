# Routing-symmetry follow-ups (catalogue)

Companion catalogue for the routing-symmetry invariant (**#1389**). The invariant
requires every adaptive, history-driven routing promotion path to have a
corresponding demotion/re-inclusion path with defined conditions, audit
attribution, and tests for both directions. See
`src/theforge/coordinator/CONVENTIONS.md` → "Adaptive routing symmetry invariant"
and `ROUTING_SYMMETRY_REGISTRY` in `src/theforge/assignment.py`.

This document catalogues the **known asymmetries that #1389 does not itself
resolve**. Each is its own follow-up story; #1389 is the framework that requires
them to exist (and be either landed or catalogued here) before further promotion
mechanisms land.

> **Tracked issues.** Acceptance criterion 3 of #1389 asks for these to be filed
> as tracked issues, each linking back to #1389. Every asymmetry catalogued here
> was filed as a GitHub issue (numbers noted per entry); this catalogue and the
> tracker agree. Keep them in sync: when an entry's inverse lands, close its issue
> and move the entry to "Resolved / not open".

## Open asymmetries

None currently open. Every registered promotion/deprioritization path in
`ROUTING_SYMMETRY_REGISTRY` has a landed, tested inverse; no pair carries an
`open_followup` marker. A new promotion mechanism that lands without its inverse
must add its entry back to this section (the enforcement test in
`tests/test_routing_symmetry_invariant.py` requires one or the other).

## Resolved / not open

### Reviewer re-inclusion path (inverse of #1388 completion-rate deprioritization) — RESOLVED (#1880)

The reviewer completion-rate deprioritization
(`assignment.py:_rerank_reviewers_by_completion`, surfaced by
`_reviewer_completion_check`, #1388) is no longer a one-way ratchet. Its paired
return path is an explicit **K-consecutive-clean-attempts** recovery rule
(`MECHANISM_REVIEWER_COMPLETION_REINCLUSION = "reviewer_completion_reinclusion"`):
a reviewer whose recency-weighted completion rate sits below
`assignment.reviewer_completion_threshold` is restored to normal ranking as soon
as the newest **K** outcomes in its `_completion_recent` ring are all clean.
`K` is `assignment.reviewer_completion_min_runs` — the same sample floor the
deprioritization is gated on — so recovery reuses the existing config surface and
demands exactly as much fresh evidence as the deprioritization did.

This matters because the two rules are not redundant: the recency weighting decays
with a ~50-run half-life, so a reviewer with a long stale failure history can
complete five straight attempts cleanly and *still* score under threshold. Passive
decay alone would leave it deprioritized for dozens of runs; the explicit streak
rule re-includes it immediately on demonstrated recovery.

**Audit attribution.** Every `routing_decision[<reviewer role>].completion_check`
carries a nested `reinclusion_check` block whenever reviewer completion profiles
were consulted — `mechanism`, `fired`, `clean_attempts_required`, `reincluded`,
`checked`, `checked_detail` (per-reviewer clean streak and below-threshold state)
and a `reason` distinguishing fired from checked-but-didn't-fire (ADR-0006
clause 7). **Registry:** the reviewer pair in `ROUTING_SYMMETRY_REGISTRY` now
names this as its `demotion` (audit label `reinclusion_check`) instead of
`open_followup="reviewer-reinclusion"`. **Tests:** both directions in
`tests/test_assignment_reviewer_completion.py`.

### Reviewer-value recovery path (inverse of the #1443/#2156 value deprioritization) — RESOLVED (#2156)

The mechanical reviewer-value deprioritization
(`assignment.py:_rerank_reviewers_by_value`, surfaced by `_reviewer_value_check`)
landed for plan review in #1443 and was extended to code-review reviewer selection
in #2156. Its paired return path is the **passive recency recovery** (ADR-0006
clause 2.4/5, `MECHANISM_REVIEWER_VALUE_RECOVERY =
"reviewer_value_recency_recovery"`): the rate the router consults *is* the
recency-weighted uniqueness rate, so a reviewer whose lifetime uniqueness sits
below `assignment.{code_,}review_value_uniqueness_threshold` climbs back above it
as fresh unique blocking findings enter the `_uniqueness_recent` ring, and the
deprioritization simply stops firing. No new config surface, no operator action,
and no permanent lock-out — the deprioritization is a sort-after, so a
deprioritized reviewer is still seated whenever no better candidate exists and
therefore keeps accumulating the samples its own recovery depends on.

**Audit attribution.** Every `routing_decision[<reviewer role>].value_check`
carries a nested `recovery_check` block whenever reviewer value profiles were
consulted — `mechanism`, `fired`, `uniqueness_threshold`, `recovered`, `checked`,
`checked_detail` (per-reviewer raw vs recency-weighted rate, sample count and
below-lifetime-threshold state) and a `reason` distinguishing fired from
checked-but-didn't-fire (ADR-0006 clause 7). The block's `phase` field records
which value history was consulted, so the plan-review and code-review checks are
never confusable. **Registry:** the reviewer-value pair in
`ROUTING_SYMMETRY_REGISTRY` names this as its `demotion` (audit label
`recovery_check`). **Tests:** both directions in
`tests/test_assignment_code_review_value.py`.

### Escalation-history decay for dev-tier promotion — RESOLVED (#158)

The dev-tier promotion no longer counts `ESCALATE` outcomes over a fixed last-10
window. `assignment.py:_check_promotion` now reads the selected dev model's
**recency-weighted** success rate at the story's complexity band from the
capability profiles (`model_profiles.get_dev_signal`, #1392) and pre-promotes only
when that rate is below the configured threshold over the sample floor
(`assignment.dev_promotion_threshold` / `dev_promotion_min_runs`). The stale-outlier
gap #1879 described is closed *by construction*: the paired return path is the
passive **recency recovery** (ADR-0006 clause 2.4/5) — as old failures age out of
the weighted ring the rate climbs back to/above threshold and pre-promotion stops
firing while admissible samples remain. That non-firing is recorded in the dev
`demotion_check` block (`_dev_recency_demotion_check`, clause 7) and registered as
the dev-promotion pair's demotion in `ROUTING_SYMMETRY_REGISTRY`
(`MECHANISM_DEV_RECENCY_RECOVERY`). #1879 is superseded by #158; no separate decay
mechanism is needed.

### Recency-weighted dev success rate — RESOLVED (#1392)

The #1389 spec's Example section listed "recency-weighted dev success rate" as a
candidate follow-up, citing `model_profiles.py:get_dev_success_rate` as a straight
cumulative `successes / total_runs` with no decay. Direct inspection shows this is
already resolved: `get_dev_success_rate` is a thin wrapper over `get_dev_signal`
and returns the **recency-weighted** rate (see `model_profiles.py`, comment citing
#1392). No follow-up is filed for this item — the spec context was stale.
