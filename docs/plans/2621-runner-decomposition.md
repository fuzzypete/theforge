# Decomposing `sprint/runner.py`'s god function

**Status:** living, 2026-08-20. This records candidate decomposition boundaries, not a
committed issue queue. Only the current slice is runnable. After each slice lands, the
remaining structure and change history are re-measured before the next issue is filed; the
issue's own body is what a sprint runs against, never this note.

## Why this note exists

ADR-0008 froze `sprint/runner.py` at 6,953 lines against a snapshot baseline of 2,721. It
is 8,286 today. #2462 extracted audit publication (`sprint/audit_publish.py`, 390 lines,
no back-dependency) and the module still grew net ~1,300 past the ceiling: extraction
works, growth outpaces it.

Measurement (at `addd2d1c`) shows the module's shape is not the shape ADR-0008's earlier
targets had. `model_profiles.py` had 78 top-level functions, largest 189 — seams existed,
only ownership was missing, so #2350/#2467 could move code across existing boundaries.
`runner.py` holds 93 top-level definitions, but one of them — `run_sprint` — is 3,677
lines, 44% of the module; the next largest is 462. The problem is a god *function*, not a
god file, and the two need different treatment:

- **File-scope extractions** (baseline gate, intake remediation, landing lifecycle, story
  execution) move already-separable top-level functions into owned modules. They shrink
  the file and clarify ownership but do not touch `run_sprint`'s body.
- **God-function slices** carve concerns out of `run_sprint` itself. These are the ones
  that change what a diff against sprint behaviour looks like, and they are the subject of
  this note.

## Doctrine

1. **One slice per filing, filed only when current.** Every slice rewrites `run_sprint`,
   so an issue specifying slice N+1 before slice N lands describes code that will not
   exist. Pre-filing the whole sequence reproduces the ratchet ADR-0008 Amendment 3
   withdrew, as a work queue instead of a gate.
2. **Boundaries by change-reason, with evidence in the filing issue.** A slice is filed
   when its concern can be shown to have changed for its own reasons (issue history), not
   because it is adjacent code. Slices below that currently lack that evidence say so.
3. **No line-count criteria.** Every slice is judged on whether a concern became
   independently changeable. Shrinkage is a consequence.
4. **The `audit_publish.py` contract.** Each extracted concern receives the execution
   state, does not import the runner back, and is testable without running a sprint.
5. **Structural work is funded by explicit decision, not floor selection.** A structural
   slice never blocks a release floor, so floor priority will never choose it. The rule
   that makes the cadence real: once a release floor is met, the operator may fund one
   structural-investment story before promotion. It never blocks promotion and is not
   selected by floor priority; funding it is an explicit investment decision. If skipped,
   the decision is recorded rather than interpreted as the candidate lacking value. The
   milestone field on a filed slice is scheduling convenience only.

## Candidate boundaries, in provisional order

These are candidates, not commitments. Slice 1 is filed and runnable. Reconciliation (2)
has change-reason evidence to assemble from real issue history; workset preparation (3)
and dispatch decisions (4) are currently architectural hypotheses and must prove their
change-reason before filing — if the re-measure after a landing shows a different concern
changing for its own reasons, the sequence is revised, not defended. The ordering
constraint that is real: later slices consume interfaces earlier ones establish.

### 1. Budget runtime enforcement — filed as #2621

The runtime half of budget enforcement: spend ledger, carried-spend restoration and
startup disclosure, pre-dispatch and in-flight enforcement moments, live budget-status
publication, budget-triggered cancellation and plan-gate release. The decision *policy*
already lives in `sprint/budget.py` (#2547) and the unmeasured-spend representation in
`sprint/unmeasured.py` (#2310); this slice moves the machinery that feeds and acts on
them, and must consume those modules rather than duplicate them.

Change-reason evidence: #1992, #2310, #2424, #2547 (the last grew the runner ~500 net
lines over five iterations). Behavioural coverage exists in
`tests/test_sprint_budget_enforcement.py`.

First because worker execution exposes budget checkpoints: slice 5 should consume the
interface this slice establishes rather than invent one.

### 2. Prior-generation reconciliation

The startup concern that converts prior-generation evidence into current execution state:
accumulated-state preload, prior outcome and landing interpretation, resume/re-exec
triage, inherited-agent and dropped-work inspection, carried failure history,
prior-generation cost attribution, skip-merged classification. Today this is a large
cluster of nested functions inside `run_sprint` (lines ~5495–6531 at `addd2d1c`).

Done means: given current tasks plus persisted prior state, reconciliation returns a
deterministic result; fresh execution bypasses it cleanly; resume and re-exec share one
set of rules rather than parallel inline branches.

Change-reason evidence to gather at filing time from the resume/re-exec issue history.

### 3. Workset preparation

Everything that turns reconciled stories into a schedulable workset: dependency
normalization, landing preconditions, intake remediation invocation, batch preflight, DAG
construction, collision edges, batch-group assignment, initial story registration. Done
means `run_sprint` receives one prepared workset value instead of constructing scheduling
inputs piecemeal.

No change-reason evidence assembled yet; this slice is the most at risk of being
boundary-by-adjacency and must not be filed until its own history is shown.

### 4. Dispatch decisions

A directly testable answer to "what should happen on this scheduler pass": wait, service
queued parent, refuse for budget, skip for auth, dispatch story, dispatch batch, declare
blocked, finish. Decisions, not threads. This is seam *creation*, not code motion — the
highest-risk slice, and the reason slices are sequenced rather than batched.

### 5. Active-worker supervision

The concurrent loop: future submission, plan-gate servicing, deadlines, timeout
cancellation, worker-exception recovery, batch fan-out, result collection, in-flight cost
recovery, handoff to landing. Consumes slice 4's decisions and slice 1's checkpoint
interface. Done means `run_sprint` contains no executor, wait loop, or future-result
classification.

### 6. Terminalization

The post-loop lifecycle: queued-PR wrap-up, terminal live-state transition, final cost
attribution, canonical counts, budget verification summary, `SprintResult` construction,
notifications, terminal audit publication, cleanup and post-sprint hook. Done means one
terminalizer consumes execution state and returns the final result with identical counts,
costs, and reasons on every exit surface.

### End state

`run_sprint` reads as lifecycle composition — construct state, reconcile, prepare,
supervise, terminalize — with `SprintRunContext` / `SprintExecutionState` / `run_sprint`
remaining the public boundary. The file-scope extractions (baseline gate, intake
remediation, landing, story execution) are a separate ownership track that can proceed in
parallel or after; they were deliberately excluded from this sequence because they do not
reduce the god function.

## Provenance

Structure measured at `addd2d1c` (AST walk: top-level definition sizes, nested-function
inventory of `run_sprint`). Sequencing derived from an external design review
(Codex, 2026-08-20) corrected on two points: the budget decision policy was already
extracted to `sprint/budget.py`, and four of its proposed module extractions were
file-scope moves that do not decompose `run_sprint`. The structural-decay observer
(#2348) ranks this path first by controlled excess spend but its global trust threshold
is unmet (#2599/#2623); it corroborates, it does not fund.
