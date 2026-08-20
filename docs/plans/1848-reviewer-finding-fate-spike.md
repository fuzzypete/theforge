# Spike: mechanical reviewer-reliability proxy from finding-fate records

Status: record (2026-08-20, issue #1848). The design and measured result are
final as of Thursday, August 20, 2026. The recommendation is **STAY OUT**. The
POC lives at `src/theforge/finding_fate_proxy/` and is not wired into routing.

## Per-fate determination

This spike is deliberately **code-review only**. It reads `finding_registry`
from the code-review audit record, not `plan_finding_registry`: the plan-review
registry serializes no `reporter`, so per-reviewer attribution is impossible
there even if future corpora carry plan-review rows.

Each sample unit is one attributable `P1` finding from a non-tainted audit
record. ADR-0006 clause-2 gating is applied at the **review-run observation**
level, not per finding: each admissible run contributes at most one addressed
share and one survived share per reviewer, so findings from the same run share
one recency age and cannot satisfy the sample floor as if they were independent
runs. The floor used is `assignment.code_review_value_min_runs` (5 in the
Thursday, August 20, 2026 corpus run), not the plan-review
`reviewer_value_min_runs`.

- `addressed`: derivable. The only structured addressed fate already written is
  final `disposition == "fixed"`.
- `dismissed`: underivable. Existing end states like `net_new`,
  `diff_ungrounded`, successful run outcomes, or `DONE` do **not** record
  "developer dismissed this finding" and are not a safe approximation.
- `contradicted`: underivable. `gate_contradicted` and `downgraded` are adjacent
  structured facts, but neither records the required event: contradiction by a
  later reviewer.
- `survived`: derivable. A finding survived subsequent review when its final
  disposition is still blocking (`unresolved`, `regression`, or `ac_blocking`)
  **and** `cycle_last_seen > cycle_first_seen`.

## POC corpus and output

The checked-in POC is:

```bash
PYTHONPATH=src python -m theforge.finding_fate_proxy --project-root /Users/pwickersham/src/theforge
```

That absolute `--project-root` is required because this worktree does not carry
its own `.forge/audits/index.sqlite`; the live substrate used for the spike
result lives in the parent repository root.

On Thursday, August 20, 2026, the POC reported:

```text
REVIEWER FINDING-FATE PROXY — SPIKE POC (#1848)

CORPUS
  project root: /Users/pwickersham/src/theforge
  records scanned: 1177
  records with code-review finding_registry: 349
  records with plan_finding_registry (ignored: no reporter): 0
  admissible records: 1160
  tainted records excluded: 17
  excluded P1 findings: 0 missing reporter, 89 no structured downstream fate
  adjacent markers: 13 gate_contradicted, 0 downgraded

ADR-0006 GATES
  sample floor: 5 admissible review-run observations
  recency: exponential (half-life 50.0, window 200)
  taint exclusion: applied at record/run level before aggregation

FATE DETERMINATION
  addressed: derivable via final disposition == fixed
  dismissed: UNDERIVABLE — needs structured per-finding developer resolution/dismissal event keyed by finding_id and review cycle
  contradicted: UNDERIVABLE — needs structured later-reviewer contradiction relation keyed by finding_id, review cycle, and contradicting reviewer
  survived: derivable via final disposition still blocking/unresolved and cycle_last_seen > cycle_first_seen

PER-REVIEWER RATES
  Rates are recency-weighted means of per-run fate shares; findings counts are reported separately.

reviewer                       findings derivable runs floor addressed dismissed contradicted survived  excl(tainted/downstream)
gemini-reviewer                     145       116   66 pass       99%       n/a          n/a       1%  0/29
openai-gpt-5.5                       75        68   49 pass      100%       n/a          n/a       0%  3/7
openai-gpt-5.5-cli                   73        63   35 pass       97%       n/a          n/a       3%  6/10
openai-reviewer                      43        39   26 pass      100%       n/a          n/a       0%  0/4
google-gemini-3.1-pro-preview        40        36   21 pass      100%       n/a          n/a       0%  0/4
codex-reviewer                       45        31   13 pass      100%       n/a          n/a       0%  0/14
openai-gpt-5.4                       23        17   10 pass      100%       n/a          n/a       0%  1/6
anthropic-opus-cli                   14        11   10 pass      100%       n/a          n/a       0%  5/3
claude-sonnet                        11        11    9 pass      100%       n/a          n/a       0%  1/0
deepseek-deepseek-reasoner           11        10    7 pass      100%       n/a          n/a       0%  0/1
deepseek-deepseek-reasoner-api        8         6    4 fail       n/a       n/a          n/a      n/a  0/2
codex-patterns-reviewer               6         5    3 fail       n/a       n/a          n/a      n/a  0/1
claude-opus                           5         3    2 fail       n/a       n/a          n/a      n/a  1/2
anthropic-haiku-cli                   3         3    2 fail       n/a       n/a          n/a      n/a  0/0
openai-api-gpt-5.4                    2         2    2 fail       n/a       n/a          n/a      n/a  0/0
claude-reviewer                       2         1    1 fail       n/a       n/a          n/a      n/a  0/1
deepseek-reviewer                     5         0    0 fail       n/a       n/a          n/a      n/a  0/5
```

The corpus shape matters more than the percentages:

- 89 attributable `P1`s had **no structured downstream fate** and therefore
  stayed excluded rather than being forced into dismissed or contradicted.
- 13 findings ended `gate_contradicted`, but that is a same-cycle gate-derived
  suppression label, not later-reviewer contradiction.
- 0 findings ended `downgraded` in this corpus, so the spike saw no structured
  P1-to-P2 cases even as an adjacent observation.

## Underivable classes and smallest missing structured event

- `dismissed` needs a structured **per-finding developer resolution/dismissal
  event** keyed by `finding_id` and review cycle. The existing record says a
  finding became `fixed` when it stopped recurring, but it never says the dev
  explicitly rejected the finding rather than addressing it, deferring it, or
  simply ending the run with no further cycle.
- `contradicted` needs a structured **later-reviewer contradiction relation**
  keyed by `finding_id`, review cycle, and contradicting reviewer. Neither
  `gate_contradicted` nor `downgraded` records that relation:
  `gate_contradicted` comes from the same-cycle gate result, and `downgraded`
  records only a severity change without a later reviewer's identity.

Two adjacent limits remain even if those events are added:

- The code-review registry stores exactly one `reporter`, so a same-cycle
  corroborated finding raised independently by multiple reviewers still credits
  one reviewer only.
- The POC imports `_recency_params()` and `_weighted_rate()` from
  `model_profiles_read_model`. That is acceptable for a spike, but admission to
  production routing would need a public helper seam instead of underscore
  imports.

## Adoption decision

**STAY OUT.**

The requested proxy is a four-class fate signal: addressed, dismissed,
contradicted, survived. Thursday, August 20, 2026 production records derive only
two of those classes mechanically (`addressed` and `survived`). The other two are
missing the very structured events that would make them bucket-B instead of
bucket-C. Shipping the proxy anyway would either:

1. publish only a partial signal while calling it the full finding-fate proxy,
   or
2. approximate dismissed / contradicted from prose-adjacent labels the story
   explicitly said not to approximate.

Neither is acceptable.

The smallest recording-cost path that could reopen admission is:

1. add a structured per-finding developer dismissal/resolution event keyed by
   `finding_id` and review cycle;
2. add a structured later-reviewer contradiction relation keyed by `finding_id`,
   review cycle, and reviewer;
3. if production routing admission is still desired after those land, promote
   `_recency_params()` / `_weighted_rate()` to a public helper and keep the
   spike's run-level gating contract.

If those events are ever added and the proxy is reconsidered for admission, the
production path should require:

1. unit tests for fate derivation and "do not approximate" exclusions;
2. substrate/taint seam tests over read-only audit loading;
3. routing-audit explanation tests showing raw counts, weighted rates, floor
   status, and excluded counts; and
4. cold-start / sample-floor tests proving no reviewer is re-ranked below the
   run-level admissibility floor.
