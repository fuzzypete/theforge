# ADR-0008: Module Size — Ratchet Rather Than Enforce or Accept

- **Status:** Accepted 2026-08-08 — **ratchet withdrawn the same day**, see Amendment 3. The measurement half stands; the blocking half is removed and its implementation deleted.
- **Date:** 2026-08-08
- **Deciders:** Peter Wickersham (project lead)
- **Affected milestones:** v0.13 (the ratchet itself), a later tech-debt milestone (bringing the frozen modules down)
- **Related issues:** #2314 (implementation), #2226 / #2284 / #2260 (recent stories whose size this ADR's evidence is drawn from)
- **Related documents:** `CONVENTIONS.md` (`max_module_lines`); `docs/vision/refusal-capability.md`

---

## Context

`max_module_lines` is 600. It is reported and never enforced: every sprint
summary since 2026-05-04 has carried the same advisory entries, naming the same
files. Over the period it has been reporting them, those files roughly tripled.

| module | 2026-05-04 | 2026-08-07 | limit |
|---|---|---|---|
| `sprint/runner.py` | 2,721 | 6,953 | 600 |
| `assignment.py` | 1,069 | 3,778 | 600 |
| `model_profiles.py` | — | 3,439 | 600 |
| `coordinator/audit_substrate.py` | — | 3,121 | 600 |

Three months of continuous, accurate, correctly-targeted reporting, during which
the reported numbers got 2.6x worse. That is the specific failure this ADR
addresses: not that the convention is wrong, and not that it is unmeasured, but
that measurement without consequence changed nothing.

This is the same shape the project already rejected elsewhere. Prompt-side
instructions to an agent are suggestions; only mechanical constraints bind. An
advisory line in a summary is a prompt aimed at the operator, and it has the
same track record.

## Why this is not only tidiness

Module size here is upstream of throughput, and the connection is measurable.

Stories now touch **9.1 files** on average, against 5–6 through the spring.
**Half** of all stories landed since 2026-07-15 touch at least one of the four
modules above. The scheduler serializes work on shared file claims, so modules
that most changes must pass through convert parallel sprints into sequential
ones regardless of how independent the issues are.

Per-story change size grew over the same period — roughly 318 insertions in
April to 791 in August — and per-story cost estimates are derived from history
that predates the growth, so they systematically under-predict the work.

Concentration is what makes size compound: a 7,000-line module that few stories
touch is ugly, while a 7,000-line module that half of them touch is a scheduling
bottleneck and an estimation error.

## Options

**A. Enforce the limit.** Make `max_module_lines` a gate failure at 600.
Mechanically simple and immediately effective. It also blocks every story
touching any of the four modules until they are split — which is most stories —
so it stops the project to pay down debt on a schedule nobody chose. Rejected as
a stop-the-world change disguised as a lint rule.

**B. Keep it advisory.** Continue reporting, act when convenient. This is the
status quo and it has a three-month record: the numbers only moved in one
direction. Rejected on evidence rather than on principle.

**C. Freeze at current size.** A module already over the limit may not grow; it
may shrink freely. A module within the limit is still governed by the limit.
New code goes elsewhere, existing violations stop compounding, and the limit
becomes reachable by attrition. Accepted.

## Decision

Adopt option C. A change that increases the size of a module already over
`max_module_lines` is refused. A change that leaves it the same size or smaller
is not. Modules within the limit continue to be governed by the limit itself,
not by their current size, so the ratchet cannot license growth up to 600 in
files that are smaller than that today.

The ceilings are derived rather than recorded by hand. A hand-maintained list
drifts from the tree by omission, and a newly created oversized module would be
governed only from whenever someone remembered to add it.

The advisory report continues to state distance from 600. The ratchet must not
make the real gap invisible by reporting only compliance with a frozen ceiling —
a module at 6,953 with a 6,953 ceiling is compliant and still eleven times the
convention, and both facts have to stay visible.

## Out of scope

Bringing the frozen modules under 600. That is larger than any one story —
`runner.py` alone is eleven times the limit — and splitting a module that half
of all stories touch is a change to the collision surface, not a refactor. It
wants its own milestone, sequenced deliberately.

Whether 600 is the right number. It is the convention this project already
adopted; this ADR is about the absence of consequence, not the value.

## What the reduction work actually is

Recorded here because the shape of it is not what the line counts suggest, and
the milestone will be scoped wrongly without it.

`sprint/runner.py` is not 6,953 lines of evenly distributed code. It is a
~3,200-line module with one function attached to the end:

| | |
|---|---|
| `run_sprint` | 3,754 lines — 54% of the file |
| other 67 top-level functions | median 30 lines |
| `run_sprint` parameters | 16 |
| functions nested inside `run_sprint` | 33, totalling 984 lines |
| distinct locals in `run_sprint` | 438 |
| locals captured by those nested closures | 117 |
| `nonlocal` names written by closures | `accumulated_cost`, `stopped_reason`, `ci_halt_slug` |

The nested functions are the reason nothing has been extracted. They are
closures over the enclosing frame — `_attempt_integration`,
`_persist_current_story_result`, `_record_dropped_story_audit`,
`_resolve_queued_pr`, `_service_plan_gates` — and three of them mutate it. They
are nested because they capture state, not because anyone preferred them there.

So `run_sprint` is already an object: 438 fields and 33 methods, none of which
have names anything can import. Moving any one of them out today means threading
five to fifteen parameters to the new home, which is how the 16-parameter
signature on `run_sprint` arose in the first place. Splitting the file without
addressing that reproduces the coupling with import statements between the
pieces, and the module-level helpers that look extractable — the intake
remediation trio, the landing helpers — are the shallow copies; the behaviour
lives in the closures.

The first unit of reduction is therefore not a file split. It is naming the
execution context those 117 captured locals constitute, and deciding who owns
cost accumulation and the sprint stop condition, which the three `nonlocal`
writes currently leave undecided. Every subsequent extraction is downstream of
that and comparatively cheap. A milestone that starts by splitting files will
spend its budget discovering this.

### `coordinator/audit_substrate.py` — a different shape, and the first one done

`audit_substrate.py` was on the table above for the same reason as `runner.py`
and for none of the same causes. It was never a god-function module: 85
top-level functions with a median of 20 lines, so the seams already existed at
function level. What it lacked was a boundary between four change-reasons
sharing one file and one owner — schema definition (8 `CREATE TABLE`), schema
migration (23 `_migrate_*`), persistence (`upsert_run_record`,
`rebuild_from_runs`, the record writers), and analytics (28 `SELECT`, 6
`derive_*`). A new query and a new migration have nothing to do with each other,
yet they landed in the same module, were reviewed together, and collided in the
scheduler as a single file claim.

Resolved by #2350 into two owners with a one-way dependency:

| module | owns |
|---|---|
| `coordinator/audit_storage.py` | connection opening and validation, schema, migrations, all record writes |
| `coordinator/audit_read_model.py` | SELECT queries and the derivations over them |

The interface between them is named: `audit_storage.AuditConnection`, a
connection storage has opened, validated, and brought to the current schema. The
read model may issue SELECT SQL against one directly — routing every query
through a storage accessor would have made storage change on every new
question, which is the coupling being removed. Storage never imports the read
model. `audit_substrate.py` remains as a re-export facade so the ~20 consumer
modules keep working.

Two things this ADR should be read as saying about that work:

**The migration catalogue was left intact.** All 23 `_migrate_*` functions moved
as one uninterrupted sequential unit into `audit_storage.py`. A sequential
catalogue is legitimately long and is meant to be read in order; dividing it
would have made it harder to read while making the line-count table look
better. An issue that judges itself on lines removed will cut exactly the part
that should be left alone.

**The success measure is independent change ownership, not line count.** The
right question is whether a new analytical query can land without touching
schema or migration code — it can, and `verdict_outcome_counts` is the worked
example, a grouped query over columns `audit_records` already indexes that
required no schema bump and no migration entry. The line-count table moved too,
but that is a side effect of the split, not the thing being bought. This is
`CONVENTIONS.md`'s rule restated: extract when a responsibility separates from
its neighbours, never to satisfy a count.

### `model_profiles.py` — the same shape, and the second one done

`model_profiles.py` is the other module the table named whose problem is
ownership rather than a god-function: 3,494 lines across 78 top-level functions
with the largest at 189, so the seams already existed at function level. Two
change-reasons shared the file — accumulation (applying a completed run's
outcome, merging it into a profile, updating stored history) and signal
derivation (the read model routing consults: per-role reliability, domain fit,
review history, observed-cost tie-breaks). Adding a routing signal has nothing
to do with how a run's outcome is folded into stored history, and the signal
side is what every future routing input reads, so the cost was set to rise.

Resolved by #2467 into two owners over one shared vocabulary:

| module | owns |
|---|---|
| `model_profiles_storage.py` | run-outcome carriers, YAML I/O, `apply_run` and its `_fold_*`/`_update_*` helpers, backfill, reset, canonical-ID migration and its `_merge_*` catalogue |
| `model_profiles_read_model.py` | `get_*_signal`, the per-domain fit signals, the observed-cost tie-break, the complexity/score stat readers |
| `model_profiles_identity.py` | what both genuinely share: role/band names, recency defaults, stored section keys, identity resolution, two stat primitives |

The dependency here runs one way in *both* directions being absent: unlike
`audit_substrate`, where the read model needs a connection storage opened, a
profile signal needs only the stored dict, so neither owner imports the other.
That is what makes the AC demonstrable — a signal is exercisable against a
profiles dict a test builds directly, with no `RunOutcome` constructed to
produce it. `model_profiles.py` remains as a re-export facade for the ~35
consumer modules and tests.

The same two readings apply as for `audit_substrate`. The per-signal
derivations were **moved, not merged**: each `get_*_signal` survives as its own
function, as does each `_merge_*` in the migration catalogue, because a module
that accumulates many small derivations is legitimately long. And the success
measure is again independent change ownership — a new routing signal is a
one-file change to the read model, and a new accumulator a one-file change to
storage, which `tests/test_model_profiles_boundary.py` pins directly rather
than inferring from the line-count table.

## Consequences

The ratchet is a holding action and should be read as one. It stops the debt
compounding and does nothing about the debt, so the throughput cost measured
above persists at its current level until the reduction work happens. Recording
that here is the point: without it, "we ratcheted it" reads as resolution, and
the four modules stay where they are indefinitely with a mechanism that makes
their size look intentional.

Expect friction on stories that legitimately belong in one of the frozen
modules. That friction is the mechanism working — it is the signal that the
module should have been split already — but it will be paid by whichever story
arrives first, which is arbitrary.

That arbitrariness is not evenly spread. `run_sprint` is where sprint-lifecycle
behaviour naturally lands, because that is where the lifecycle is; half of all
stories already touch one of the four frozen modules. So the ratchet will bite
first and hardest on exactly the work most likely to arrive, and the story that
pays is the next one that needs to change how sprints execute — which is a
routine kind of story, not a rare one.

The available response to that friction also degrades. Early on, a blocked story
can put its code in a new module. As the obvious new homes are taken, the
remaining option is to reduce the frozen module by at least what the change
adds, which means doing a piece of the reduction work as a side effect of
unrelated work, un-sequenced and unscoped. That is the failure mode to watch: if
stories start carrying incidental extractions to get under their ceiling, the
ratchet has stopped being a holding action and become an unplanned refactor
distributed across whoever happened to be blocked.

## Amendment 2026-08-08: the cost is spend, not only scheduling

The Context section above argues module size costs throughput, via collisions that serialize
parallel work. Spend data gathered after this ADR was accepted says that is the smaller half.

Average cost per run, from the audit substrate:

| month | avg $/run | avg complexity | avg duration | success rate |
|---|---|---|---|---|
| 2026-04 | $2.81 | 5.9 | 18 min | 73% |
| 2026-05 | $3.79 | 5.7 | 19 min | 79% |
| 2026-07 | $7.26 | 5.5 | 14 min | 73% |
| 2026-08 | $12.19 | 6.4 | 20 min | 76% |

Cost per run rose 4.3x while complexity, duration and success rate stayed flat. It is not an
artifact of failures: restricted to successful runs alone, the same period goes $3.00 to $11.33.

It decomposes into two factors that account for the increase with no residual:

| | 2026-04 | 2026-08 | factor |
|---|---|---|---|
| invocations per run | 2.8 | 5.1 | 1.8x |
| cost per invocation | $1.00 | $2.39 | 2.4x |

The first factor is review panel growth — review calls per run went 1.8 to 3.4. The second is the
one this ADR is about, and git corroborates it independently of the audit substrate:

| | 2026-04 | 2026-07 | factor |
|---|---|---|---|
| files per merge | 5.9 | 10.3 | 1.7x |
| insertions per merge | 351 | 1,475 | 4.2x |

Stories did not get harder; the changes they produce got four times bigger, landing in modules that
tripled over the same window. Every reviewer reads that diff and the modules around it, so change
size and module size multiply into token cost directly. Duration stayed flat because the work is not
slower — more tokens flow through the same number of minutes.

Routing was ruled out as the explanation. The strong-tier model now takes roughly 95% of dev runs
where it took a minority in April, but the split is complexity-appropriate — strong tier draws
complexity 4-9, cheap tier 1-3 — and the cold-start starvation bug that could have concentrated
selection artificially closed in v0.12.0 (#1617). Complexity 7-9 work did grow from 37% to 50% of
the queue, which is real and nowhere near sufficient to explain 4.3x.

Two consequences for how the reduction milestone is justified and scoped.

The argument is spend, not tidiness, and it compounds without an upper bound that anything currently
enforces. The ratchet freezes module size and does not touch change size, so the 4.2x factor keeps
running after this ADR's decision is fully implemented.

Nothing measures or bounds per-story change size today. A story landing 1,475 insertions across 10
files passes every gate. That is the upstream cause of the larger factor and it is not what the
ratchet addresses, so it wants naming as separate work rather than being assumed to fall out of
module reduction.

## Amendment 3 (2026-08-08): the ratchet is withdrawn

It was enforced for one day. The first story to meet it, #2309, paid $33.05 and an
entire extra dev cycle, and produced worse structure than if it had never run.

#2309 is a process-teardown story, so `process_group.py` legitimately had to grow — by
413 lines. Cycle 1 wrote correct code and passed the gate. It was bounced anyway on 8
blocking convention violations, 7 of them this ratchet, of which four were overages of
6, 12, 12 and 30 lines on modules already 1,200–3,200 lines long. Cycle 2 bought nothing
functional: it relocated 413 lines into `process_tree.py` plus lease/sidecar/teardown
units, each of which then needed a test mirror, and passed the same gate it had already
passed. Two thirds of the story's dev spend went to redistributing code.

| cycle | cost | duration | gate | outcome |
|---|---|---|---|---|
| 1 | $16.85 | 15m 10s | PASS | bounced — 8 blocking convention violations |
| 2 | $33.05 | 26m 56s | PASS | clean → review |

The Consequences section above predicted this precisely — "if stories start carrying
incidental extractions to get under their ceiling, the ratchet has stopped being a
holding action and become an unplanned refactor distributed across whoever happened to
be blocked" — and predicted the target, that it would bite first and hardest on work
that changes how sprints execute. What the section treated as a risk to watch for was
the first observed behaviour, not a tail case.

### Why it could not have worked

The failure is structural, not a threshold that wanted tuning. Any blocking line-count
check at implementation time is satisfiable by cutting anywhere, and cutting anywhere is
always cheaper than cutting at the right seam. So the agent's minimum-effort response is
not misbehaviour to be corrected — it is the constraint's definition of success. A cap
on new modules fails identically, one step earlier: a story needing a 700-line module
ships 480 + 220 instead.

This generalises. Module cohesion is a **codebase-scoped, temporal** property: it
degrades across a hundred stories each individually correct against its own spec. Every
forge gate is **story-scoped**. A story-scoped gate cannot enforce a codebase-scoped
property; it can only enforce a proxy, and the proxy is what gets optimised. The forge is
a closed loop over stories and an open loop over architecture, and the ratchet was an
attempt to close the second loop with the first loop's machinery.

That is also the boundary of the project's mechanical-over-prompts tenet, which is worth
stating because this looks like a counterexample and is not. Mechanical guards bind when
the property is binary and checkable — spend ceilings, scope, destructive operations.
Cohesion is neither, so a mechanical proxy for it does not enforce cohesion. It enforces
the proxy.

### What replaces it

Nothing, at the gate. `check_line_counts` is unchanged and still reports all 48 modules
over 600 with their distance from the limit.

That is deliberately the pre-ADR state this document was written to condemn, and the
Context section's judgement of it stands: three months of accurate reporting during which
the numbers got 2.6x worse, because measurement without consequence changes nothing. The
conclusion to draw is narrower than it first appears. The error was not "reporting is
insufficient, therefore block." It was assuming the only available consequence is a
refusal. A refusal is the consequence that must be paid immediately, by whoever is
present — which is exactly why it produced arbitrary seams charged to arbitrary stories.

The consequence this wants instead is **funded work entering intake**: a periodic
structural observer that reads the audit substrate — cost per module, collision
centrality, co-change clusters, bounce and escalation concentration, structural signals
like captured state and parameter growth — and emits candidate discovery spikes, which
are then groomed like any other intake. Size is one input to that ranking and a weak one;
a 3,200-line module nothing touches is low-priority, while a 1,200-line module in every
sprint's collision path is not.

Two constraints on that observer, both learned here. Participation is not causation — a
$40 story touching ten files does not attribute $40 to each, so ranking needs excess
spend against comparable stories, not raw totals. And its emission rate must be capped at
one open candidate at a time; a ranking function generates architecture candidates faster
than a solo operator funds them, and an unfunded backlog is the 48-issues problem
arriving through a side door.

Until that exists, module size is measured and inert. That is a worse steady state than a
working consequence and a better one than this ratchet, and naming it as a gap rather
than a resolution is the point of recording it here.

### Withdrawn in code

`module_growth_violations`, `module_line_ceiling` and `fail_closed_module_violations` are
deleted rather than left dead. `coordinator/convention_baseline.py` remains: it still
resolves the branch point for the rules that do block. Verified against the tree at
withdrawal: 48 advisory findings, 0 blocking.

## References

- `CONVENTIONS.md` — `max_module_lines`
- Sprint summaries, `advisory_convention_violations`, first seen 2026-05-04
- #2314 — implementation
- #2309 — the story that paid for the ratchet; evidence for Amendment 3
- #2325 — first reduction unit (naming `run_sprint`'s execution context)
- #2349 — the same unit for `assignment.py`: naming the routing evidence its
  routing pass accumulates (`theforge/routing_evidence.py`)
- #2399 — the same unit finished: the state carries what the frame held, the five
  named closures are module-scope, and `run_sprint` takes the context
- #1617 — cold-start starvation, closed v0.12.0; ruled out as a cause of the model-mix shift
- Amendment figures: `.forge/audits/index.sqlite` (`audit_records`, `invocation_identities`,
  `reviews`); `git log --merges --shortstat` for the per-merge columns
