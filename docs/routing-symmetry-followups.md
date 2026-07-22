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
> as tracked issues, each linking back to #1389. Both open asymmetries below have
> been filed as GitHub issues (numbers noted per entry); this catalogue and the
> tracker agree. Keep them in sync: when an entry's inverse lands, close its issue
> and move the entry to "Resolved / not open".

## Open asymmetries

### 1. Escalation-history decay for dev-tier promotion — #1879

- **Promotion path:** `assignment.py:_check_promotion` — 2+ `ESCALATE` outcomes in
  the last 10 matching records bump a story's dev tier upward.
- **Gap:** the matching slice is recency-bounded (last 10) but never *decays*. A
  single outlier escalation persists in that window for a long time on infrequent
  complexity bands, keeping the tier promoted after subsequent runs succeed
  cleanly.
- **Proposed inverse:** exclude escalation records older than N records or M days
  from `_check_promotion` matching, so stale escalations stop holding the tier up.
- **Tracked issue:** #1879 (back-links #1389; companion to #1387).

### 2. Reviewer re-inclusion path (inverse of #1388 completion-rate deprioritization) — #1880

- **Deprioritization path:** `assignment.py:_reviewer_completion_check` (#1388) —
  a reviewer with a poor attempt-completion history is reranked down.
- **Gap:** there is no landed mechanism that re-includes a deprioritized reviewer
  once its subsequent attempts complete cleanly. #1388 (reviewer attempt-completion
  profiles) has landed, so this inverse is now actionable.
- **Proposed inverse:** re-include a deprioritized reviewer after K consecutive
  clean attempts, with audit attribution and tests for both directions.
- **Registry marker:** `open_followup="reviewer-reinclusion"` on the reviewer pair
  in `ROUTING_SYMMETRY_REGISTRY`.
- **Tracked issue:** #1880 (back-links #1389; companion to #1388).

## Resolved / not open

### Recency-weighted dev success rate — RESOLVED (#1392)

The #1389 spec's Example section listed "recency-weighted dev success rate" as a
candidate follow-up, citing `model_profiles.py:get_dev_success_rate` as a straight
cumulative `successes / total_runs` with no decay. Direct inspection shows this is
already resolved: `get_dev_success_rate` is a thin wrapper over `get_dev_signal`
and returns the **recency-weighted** rate (see `model_profiles.py`, comment citing
#1392). No follow-up is filed for this item — the spec context was stale.
