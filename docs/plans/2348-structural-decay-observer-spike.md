# Spike: structural-decay observer — design, POC, and adoption decision

Status: record (2026-08-15, issue #2348). The design and the measured result are
final as of this date; the recommendation is **DEFER**, and the reopen condition
is stated in [Adoption decision](#adoption-decision). The POC lives at
`src/theforge/structural_decay_observer/` and is not wired into anything.

## The gap

TheForge is a closed loop over stories and an open loop over architecture. Every
gate, review and refusal operates at story scope; architecture degrades across a
hundred stories that were each individually correct against their own spec. No
component looks at the codebase across time and produces work, so structural
decay has no path into intake except an operator noticing it.

ADR-0008 tried to close that with story-scoped machinery — a blocking module-size
ratchet — and Amendment 3 records why it could not work: a story-scoped gate can
only enforce a proxy, and the proxy is what gets optimised. Module size is now
measured and inert (48 modules over the limit, reported every run, blocking
nothing), which is the pre-ADR state ADR-0008 was written to condemn.

The replacement consequence is *funded work entering intake* rather than a
refusal paid by whoever happens to be blocked. This spike is the design work for
that consequence, plus a POC to validate or kill it.

## Design

### Seam

The observer is a **read model over the audit substrate** and nothing else. It
reads through `open_readonly()` (ADR-0002) and issues SELECTs against
`audit_records` joined to `audit_changed_files` — the table #2347 added. It does
not touch `audit_storage` schema, migrations, or any write path, and it does not
participate in the run lifecycle. Two SELECT-only helpers were added to
`coordinator/audit_read_model.py` for it:

- `changed_file_touch_rows()` — the bulk form of `runs_touching_path()`: every
  `(run, path)` touch joined to the run's indexed columns in one scan, filtered
  to runs with a *measured* positive cost. A cost-unknown run is a lower bound on
  what the work needed, not a measurement of what it cost, so averaging one in
  understates every path it touched.
- `changed_file_coverage()` — the denominator: how many measured cost-bearing
  runs, and how much measured spend, actually join to a changed-file set.

`changed_file_coverage()` is the load-bearing one. Without it a ranking computed
over the joinable minority silently presents itself as a ranking of the codebase.

### Signal set and the controls applied to each

The ranked quantity is **controlled excess spend**, not spend. Participation is
not causation: a $40 run touching ten files does not attribute $40 to each.

Cost is *attributed* — run cost divided evenly across the files the run changed —
and then *controlled* against comparable runs that did **not** touch the path.
Even division is a stated simplification, not a measurement; the substrate
records what a run cost and which files it touched, never how spend was
distributed between them. It is used because the alternatives are worse: charging
the full run cost to every file multiplies one run's spend by its file count, and
weighting by insertions rewards verbosity. See
[Methodological defect](#methodological-defect-found-by-the-poc) — this choice
turned out to be the POC's biggest problem.

| Control | Availability | Source |
|---|---|---|
| complexity score band (1–3 / 4–6 / 7–10) | `indexed` | `audit_records.complexity_score` |
| dev model / tier proxy | `indexed` | `audit_records.dev_resolved_model` |
| reviewer panel size | `derived` | `raw_json.reviews[].pool_models` |
| intended feature size (files changed) | `derived` | `audit_changed_files` row count |

Availability is reported per control on every run, and an `unavailable` control
is forced into the weakest-signal candidate set rather than silently dropped. The
difference between "we controlled for panel size" and "we could not" is the
difference between a residual and a raw total, and a report that cannot tell them
apart is not challengeable.

Controls relax in a fixed order — intended size, then panel size, then dev model —
until the cohort reaches `MIN_COHORT_RUNS`; whatever survived is recorded per
comparison, so a comparison that ended up controlling for nothing reports an
empty tuple. A run that reaches no cohort at any level contributes **zero**
excess, never an imputed figure.

Secondary evidence carried per candidate, none of it ranked on: touch count,
attributed spend, line count, co-touch centrality (the collision proxy), review
findings naming the file, and review-cycle count.

### Weakest signal

Every candidate names the single most limiting fact behind it, chosen in
most-disqualifying-first order: below the touching-run floor → no cohort at any
level → some runs uncontrolled → a control unavailable → thinnest cohort size.

One sentence, not a list. It is read next to the number it qualifies, and a list
of caveats gets skimmed — a candidate whose weakest signal is skimmed is a
candidate that gets rubber-stamped, which is the failure mode this whole design
is trying to avoid.

### Trust threshold

The spike's answer to "how much recorded data before a ranking means anything",
encoded as named constants the POC checks itself against:

| Floor | Value | Why |
|---|---|---|
| `MIN_RUN_COVERAGE` | 0.80 | Below this the ranking describes the joinable minority, not the codebase |
| `MIN_JOINABLE_RUNS` | 30 | Fewer than ~30 measured runs cannot populate cohorts across four control dimensions |
| `MIN_TOUCHING_RUNS` | 10 | A per-path excess figure from single-digit runs is an anecdote with a dollar sign |
| `MIN_COHORT_RUNS` | 5 | A median over fewer than 5 comparables is one outlier away from meaningless |

These are reported as four independently-checked conditions rather than one
boolean, because *which* floor is short is the actionable part — it distinguishes
"wait for more runs" from "the join itself is broken".

### Emission surface and cadence (designed, not built)

Had the decision been *build*, the emission design is: a manual
`todo:draft` discovery issue, **at most one open structural candidate at a time**,
the next emitted only when the prior closes. Rejection is suppressed by *recorded
operator decision* on the candidate, not by hiding future evidence — the path
stays rankable and re-surfaces if the evidence materially strengthens, because
"the operator said no once" and "this is not a problem" are different claims and
only the substrate can distinguish them.

The failure mode being designed against is volume: a ranking function generates
candidates faster than a solo operator funds them, and an unfunded backlog of
structural suggestions is the 48-issues problem arriving through a side door.

## POC

`python -m theforge.structural_decay_observer [--since DATE] [--top N]`

Pure-Python, read-only, imported by nothing. `ranking.py` is the math (no I/O,
tested against seeded rows); `report.py` is substrate loading, rendering and CLI.
A test asserts nothing in `src/theforge/` imports it — if that fails, the POC has
become a component and needs the design review it has not had.

## Measured result (2026-08-15)

```
COVERAGE
  31 of 160 measured cost-bearing runs join to a changed-file set (19.4%)
  $301.80 of $2,230.47 measured spend (13.5%)
  window: 2026-08-12T16:44:55Z .. 2026-08-15T17:49:30Z

CONTROLS
  complexity score band                    indexed
  dev model / tier proxy                   indexed
  reviewer panel size                      derived
  intended feature size (files changed)    derived

TRUST THRESHOLD: NOT MET
  [FAIL] run coverage: 31 of 160 measured cost-bearing runs join (need 0.80)
  [ok  ] joinable sample: 31 joinable measured runs in window (need 30)
  [FAIL] ranked candidate floor: 0 path(s) reach 10 touching runs (need 10)
  [ok  ] controlled comparison floor: 80 path(s) obtained a cohort of >=5 (need 1)

RANKING BY CONTROLLED EXCESS SPEND (top 3)
  1. src/theforge/cli/provider_readiness.py
       8 touching run(s), $10.74 attributed spend
       excess: $+1.84 over 8 controlled comparison(s)
       723 lines
       co-touched with: providers.py (8), test_cli_setup.py (7), test_provider_readiness.py (7)
       named in 2 review finding(s)
       weakest signal: 8 touching run(s) is below the sample floor of 10 for a
                       tier-controlled comparison; treat the excess figure as
                       directional, not measured
  2. src/theforge/cli/providers.py      8 touches, +$1.84,  188 lines
  3. src/theforge/cli/sprint_digest.py  3 touches, +$1.55,  688 lines

COMPARISON AGAINST PURE LINE-COUNT RANKING (the ship gate)
  paths compared: 80
  spearman rank correlation: -0.241
  top-8 overlap: 0 (0%)
  by excess:     cli/provider_readiness.py, cli/providers.py, cli/sprint_digest.py
  by line count: sprint/runner.py, assignment.py, sprint/rca.py
  biggest rank movers: cli/providers.py (+72), routing_evidence.py (+68),
                       sprint/runner.py (-67)
```

### Does it beat `wc -l`?

**It is not `wc -l` — and it is not right either.** Both halves matter.

Spearman correlation between the excess ranking and the line-count ranking is
**−0.241** over 80 paths, with **zero** overlap in the top 8. The ranking has
demonstrably not rediscovered module size.

But the spec set a second, harder test: it must *find known pain* —
`sprint/runner.py`, `assignment.py` — without sorting by size. It does not.

| Path | Lines | Touches | Excess | Excess rank |
|---|---|---|---|---|
| `src/theforge/sprint/runner.py` | 7,691 | 7 | **−$0.85** | 68 of 80 |
| `src/theforge/assignment.py` | 4,337 | 3 | **−$0.09** | 40 of 80 |
| `src/theforge/coordinator/engine.py` | 2,086 | 2 | **−$0.39** | 58 of 80 |

The three modules everyone already knows are the problem rank in the bottom half
with *negative* excess. The negative correlation with size is not the ranking
being smarter than `wc -l`; it is the ranking being **actively wrong** about the
cases where the answer is already known.

### Why: the window is three days

`audit_changed_files` only carries data for runs since #2347 landed. The joinable
window is **2026-08-12 to 2026-08-15** — 72 hours, 31 runs, 80 touched modules,
of which 46 were touched exactly once and *none* reached the 10-touch floor. The
maximum touch count in the entire dataset is 8.

So the ranking is not measuring decay across time. It is measuring **what was
worked on in the last three days**, which happens to have been provider-readiness
CLI work. `sprint/runner.py` ranks low because a 3-day window cannot show that a
7,691-line module has been serialising sprints for months.

### Methodological defect found by the POC

Independent of the data thinness, the POC surfaced a real flaw worth recording
before anyone re-runs this:

**Even attribution systematically penalises exactly the modules of interest.**
`attributed_cost = run_cost / files_changed` means a large central module touched
as part of a wide 12-file change is attributed 1/12 of that run's cost, while a
narrow 2-file change attributes half. Large central modules are precisely the
ones touched in wide changes, so the metric structurally biases *against* them.
`sprint/runner.py`'s negative excess is partly this artifact, not a finding.

The intended-size control was supposed to absorb this by comparing wide changes
only to wide changes. On 31 runs it usually cannot — it is the first control
dropped during relaxation, so most comparisons never applied it.

Any revival must fix attribution first. The direction to try: attribute by
*share of the change* (this file's insertions+deletions over the run's total)
rather than evenly, and treat intended size as a non-droppable control rather
than the first one relaxed.

## Adoption decision

**DEFER.**

Not *build*: no floor for a trustworthy ranking is met, the method demonstrably
mis-ranks the three known-pain modules, and shipping an observer on this evidence
would emit candidates an operator would learn to ignore — reproducing ADR-0008's
failure with a nicer surface.

Not *abandon*: the method has not been falsified, only left untested. Every
negative result above is fully explained by a 72-hour window in which no path was
touched more than 8 times. Killing a design on data that cannot discriminate
would be the same error as shipping one on it.

### The condition that reopens this

Re-run `python -m theforge.structural_decay_observer` when **all four** hold:

1. `run coverage >= 0.80` — at least 80% of measured cost-bearing runs join to a
   changed-file set. Currently 19.4%; this rises automatically as pre-#2347 runs
   age out of the denominator.
2. At least **30 joinable measured runs** — already met (31).
3. At least **one path reaching 10 touching runs**. Currently 0; max is 8. This
   is the real gate, and it needs a window measured in months, not days.
4. A joinable window spanning **at least 90 days**. Currently 3.

Realistically: revisit no earlier than **2026-11-12** (90 days after #2347's
first joinable run), and only if condition 3 holds by then.

### What would kill it instead

If, at that point, the ranking correlates *positively and strongly* (Spearman
> 0.7) with line count, it has recreated the metric ADR-0008 withdrew and should
be abandoned rather than shipped. The POC reports that number on every run
specifically so the kill signal is visible rather than buried.

### Disposition of the code

- `changed_file_touch_rows()` / `changed_file_coverage()` in
  `audit_read_model.py`: **keep** regardless of the outcome. They are general
  substrate queries — "what does this file cost" and "how much spend can we
  attribute to code at all" — useful to any future analysis and to answering the
  coverage question by hand. They are SELECT-only and carry no observer concepts.
- `src/theforge/structural_decay_observer/`: **keep as a spike artifact** until
  the reopen date, then either revived (with the attribution fix above) or
  deleted along with its tests. It is inert, tested, and costs nothing to carry;
  deleting it now would mean re-deriving the methodology from this document in
  three months.

## Sizing, if revived

Not funded now, but the shape is known: fix attribution and the relaxation order
(~1 story), then emission surface + one-open-candidate cap + rejection recording
(~1 story), then the cadence decision. The observer itself is small; the design
questions this document answers were the expensive part.
