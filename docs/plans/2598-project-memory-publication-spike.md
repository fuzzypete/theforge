# Spike: project-memory publication path and landing-evidence model

Issue: TheForge #2598. Status: **build** (see [Adoption decision](#adoption-decision)).
Validating proof of concept: `tests/test_protected_base_publication_poc.py`.

## The question

Forge publishes a project's shared memory — canonical run records under
`.forge/audits/runs/`, knowledge summaries under `.forge/knowledge/summaries/` — by
committing them directly onto the base branch in the project-root checkout at the end of a
sprint. A repository that requires its base branch to advance by merge or pull request
never receives its own memory:

```
✗ SPRINT  canonical story run audit publish failed: Failed to commit story run audits
at .forge/audits/runs, knowledge summaries at .forge/knowledge/summaries:
⛔ COMMIT BLOCKED: Non-doc changes on 'main'.
```

The failure is not recoverable by retry, because the operation forge attempts next sprint
is the one the policy refuses. The workaround adopted downstream was an allowlist entry
granting forge direct-commit rights on a protected branch — a privilege escalation to work
around a publication path.

The second half of the question is why the same investigation surfaced it. Forge stored a
`landing_status` field in each run record and rewrote the record after integration so it
carried the resolved landing result. That is a denormalization of git state into the
knowledge corpus, and it has drifted from git twice: #2374 read a commit merely
*mentioning* an issue as evidence its branch merged, and #2111 re-triaged a landed story
as stale.

These are one question. Where a record is published determines what can be concluded from
its presence, and therefore whether a separate landing assertion is needed at all.

## The chosen model: two channels

**Channel 1 — project memory.** Immutable run records, knowledge summaries and landing
evidence are published through a *mode-safe transport*. They are what the run produced, and
they are published whether or not the run landed.

**Channel 2 — landing evidence.** A successful landing, once observed, creates a separate
artifact that names what landed where. Nothing else creates one. A landing that does not
happen creates an *attempt* artifact, which records the failure without rewriting the run's
record into a negative claim — nothing about the run changed, only the world's response
to it did.

The two channels travel together (the evidence tree is project memory, published by the
same transport) but they are produced by different events, at different times, by
different observers. That separation is the answer to both halves of the question: the
presence of a record on a branch says only that a run happened, and a landing assertion is
a *consequence* of a landing rather than a claim written about one.

`landing_status` is **not** removed. It remains the sprint scheduler's in-process answer to
"may I release this story's dependents yet", which is a live scheduling question. What
changes is that the durable answer no longer comes from it, and that the *record* never
carries the negative half of it: a canonical run record's `landing_status` is `landed` or
"a landing was owed" (`pending_integration`), never `failed`. That is enforced on creation
as well as on replacement — a story whose landing fails on its first attempt has no earlier
record to preserve, and guarding only rewrites would let exactly that story's record be
created carrying the claim.

Downstream flattened readers keep working for the positive case, which is the one they
mostly ask about: `coordinator/issue_cost.py` still labels landed runs, and `cli/audits.py`
still prints a landing column. What they lose is the negative detail —
`cli/sprint_digest.py` reads merge evidence from the record's `landing` dict, and for a
failed landing that dict is now empty rather than describing the failure. The failure is in
the attempt artifact instead, and re-pointing those readers is [follow-on 4](#follow-ons);
until then a failed landing reads as unresolved there, which is the honest degradation of
the two.

### Constraints, checked

| Constraint (from the story) | How the model satisfies it |
| --- | --- |
| The corpus accumulates | Records are still produced, still tracked, still pushed. Only the *carrier* changes: a long-lived memory branch and a pull request instead of a direct base commit. `tests/test_memory_publication.py::test_a_fresh_clone_of_the_memory_branch_carries_the_corpus` asserts it from a clone rather than from a push result. |
| Records exist for runs that did not land | Publication is unconditional on landing. Escalated, failed and refused runs publish their records; the matrix below has no cell where a record is withheld. |
| Positive landing evidence is created only by a successful landing, and names what landed where | `build_landing_assertion` requires run, slug, landing mode, target branch, reviewed commit, gated commit, carrier kind, carrier reference, landed commit, observer and observation time. There is no way to construct one without them — a caller who only knows a landing was *requested* cannot express that through this API. |
| Ancestry alone cannot supply that evidence | Under `merge_strategy: squash` the reviewed commit is not an ancestor of the target branch. The observers consult GitHub's `mergeCommit.oid` first for that reason, and fall back to git topology only where the reviewed commit is provably reachable. Where neither can name a commit, the observation records `unknown` and stops. |
| Review and gate attestations are keyed to a commit | Nothing is written into the story's branch. Project memory goes to a *separate* branch, so the commit that merges is exactly the commit that was reviewed and gated. The assertion records both attested commits alongside the landed commit precisely because a squash makes them differ. |

## The publication path

### Transport selection

```
memory_transport(lands_locally=…)  →  "direct" | "memory-branch"
```

The question is not which `on_approve` mode is configured but whether **this run advances
the base branch from the project-root checkout at all**:

* A run that does (`on_approve: merge`, or `--auto-merge` in sequential mode) is already
  committing there. A memory commit alongside its merges is consistent with what it does
  anyway, and keeps the corpus on the base branch with no review latency. → `direct`.
* A run that does not (`merge-pr`, `pr`, `none`) reaches the base branch only through pull
  requests, and a direct memory commit would be the *one* thing forge does to that branch
  without one. → `memory-branch`.

Keying on the landing behaviour rather than on the mode string matters: `--auto-merge`
forces the local-merge path regardless of `on_approve`, and a selector that read only the
mode would have sent those runs down the wrong transport.

**The choice is not final in either direction.** A direct commit or push that the base
branch *refuses* (`commit_failed`, `push_refused`) falls back to the memory-branch
transport. That is what makes the reported `merge`-mode failure recoverable without
granting forge direct-commit rights on a protected branch. The fallback is only allowed to
downgrade the original error when it actually got the corpus off the machine
(`published` / `pushed_without_pr`); otherwise the original failure is re-raised, because a
sprint must not exit reporting success over unpublished base-branch state.

### Staging, and why it is separate from publishing

`stage_pending_project_memory` drains everything pending in the tracked memory trees out of
the project-root working tree into `.forge/memory-staging` (ignored by `.forge/**`), leaving
the checkout clean. Artifacts absent from `HEAD` are moved; artifacts present in `HEAD` are
copied and restored from it, because moving one leaves a deletion behind. Presence in
`HEAD` is the test rather than presence in the *index*, because a commit refused by a hook
leaves the artifacts staged in the index, and a restore keyed to the index would restore
exactly the content being drained.

Draining is separated from publishing because they fail independently and only one of them
is urgent. A dirty project root refuses a landing, so before this change a transport that
could not commit was also a transport that stranded the next story. Now the root is clean
the moment the artifacts are staged, whether or not the publish that follows succeeds; a
failed publish *retains* staging rather than putting anything back.

That separation also relaxes a scheduling constraint. The mid-sprint publish (#2595) had to
wait for a quiescent pass, because it committed from the same checkout a concurrent local
merge was using. The constraint belongs to project-root *landing*, not to publishing, so it
is now keyed to `config_lands_in_project_root`: a run with no local landings publishes on
every pass, which is what keeps a finished story's artifacts from standing across its
sibling's entry under `--parallel`.

**A pass-level publish is not sufficient on its own.** It runs once per scheduling pass, so
a sibling that finishes *during* a pass leaves its record in the shared checkout for the
next story admitted from the same `ready` snapshot — a window the pass-level publish cannot
close, and one this spike's own proof of concept hit intermittently before it was closed.
So each admission is preceded by a *drain* (`drain_project_memory_before_dispatch`): the
staging half alone, with no branch, commit or push, which is what makes it affordable at
per-story frequency. Publication stays at the pass level. This is only done for runs that do
not land in the project root — the same condition that lets them publish without waiting for
a quiet pass — because a run that lands locally publishes by committing the working-tree
copies, and draining them would hide the memory it is about to publish.

### Nothing transient may live in a tracked tree

Both canonical writers wrote their atomic-replace temporary file *inside* the tree they were
writing to: `.forge/audits/runs/<run>.tmp`, and the same shape for landing evidence. Those
trees are re-included by the generated `.gitignore` precisely so they are tracked, which
made a write-in-progress file indistinguishable from project memory. It dirtied the shared
checkout for as long as the write took — enough to refuse a sibling story's landing — and
the transport would have carried it into the corpus as a half-written record.

Both now write to `.forge/audits/.tmp/`, denied by the same `.forge/**` rule and re-included
by nothing, on the same filesystem so the replace stays atomic. The transport additionally
refuses to treat any `.tmp` path as publishable memory, for repositories carrying strays
from writers that pre-date this.

### The memory branch

One long-lived branch, `forge/project-memory`, updated in place, with one pull request into
the base branch. The alternative — a branch and PR per sprint — converts a publication
problem into a review-queue problem against a base the operator has already said they gate
carefully.

* **Start point.** The existing remote memory branch when there is one, so unmerged memory
  from an earlier sprint is carried forward. The base branch when the memory branch's
  content is already an ancestor of it — the pull request merged, and continuing from a
  branch whose commits are all in base would grow a permanently-behind branch for nothing.
* **Isolation.** Published from a temporary git worktree. The project-root checkout is read
  but never committed to and never has its branch moved, so a story landing at the same
  moment sees a checkout the publish has not touched.
* **The PR carrier.** Opened with `gh` if one is not already open; reused if it is. `gh`
  being absent or unauthenticated is not an error — the branch is published anyway and the
  end state says exactly that (`pushed_without_pr`), which is more useful than a failure
  that says nothing about what did work.

**Who merges the memory PR: the operator.** Forge does not auto-merge it. Auto-merging
would be forge merging into the protected branch on its own authority, which is the
privilege this design exists to stop needing. The consequence has to be stated plainly:

* While the PR stays open, published memory lives on the memory branch, not on base.
* Forge's own readers therefore also read `.forge/memory-staging`, which is retained after
  a successful publish for exactly this reason and pruned of anything the base branch has
  since absorbed. Without that read-through, reconciliation would go looking again for a
  landing it had already observed.
* A memory PR left open across N sprints accumulates N sprints of memory in one PR. It
  never blocks a sprint, and it never grows the checkout, but a *fresh clone of the base
  branch* is missing everything in it. The operator-facing signal today is the publish end
  state recorded in `.forge/audit-publish-state.json` (`memory_branch_published`) plus the
  PR itself. **A standing "memory PR open for N sprints" warning is not built and is the
  first follow-on below.** The proof of concept resolves the PR immediately and therefore
  does not exercise the stuck case.

## The landing-evidence model

Two artifact kinds, under `.forge/audits/landing/` — a sibling of the runs tree, tracked
by the same generated `.gitignore` re-includes, so it travels to a fresh clone.

**Positive landing assertion** (`<run>.landed.json`) — created only by an observed
successful landing. Names: `run_id`, `slug`, `landing_mode`, `target_branch`,
`reviewed_commit`, `gated_commit`, `carrier_kind` (`pull_request` | `merge`), `carrier_ref`,
`landed_commit`, `pr_url`, `observer`, `observed_at`. **Write-once**: a landing happens a
single time, and silently replacing the first assertion with a second would make the
artifact exactly the rewritable claim it replaced.

**Landing attempt** (`<run>.attempt-NNN.json`) — created by any landing attempt.
`outcome` is one of a closed set: `queued`, `refused`, `failed`, `timeout`, `closed`,
`unknown`. There is deliberately **no spelling of "landed"** available, so no attempt can be
mistaken for evidence whatever value it carries. Attempts accumulate rather than collapse: a
landing refused on sibling dirt and then retried made two attempts, and erasing the retry
erases what the operator needs to see.

Attempts carry `source_commit` (the reviewed commit) and `gated_commit` even though they
assert nothing about them. That is what lets a *later* observer promote an attempt into an
assertion: the attestations are keyed to those commits, only the process that ran the story
knows them, and a reconciliation running after sprint exit has the attempt and nothing else.

### The read model

`landing_state(project_root, run_id)` returns exactly three values:

* `landed` — a positive assertion exists.
* `unresolved` — no assertion, and either no attempt or an attempt still open
  (`queued`, `unknown`).
* `not_landed` — no assertion, and the last attempt reports a resolved non-landing
  (`refused`, `failed`, `timeout`, `closed`).

A run with **no evidence at all is `unresolved`**. Defaulting it to either landed or failed
is how a read model invents facts, and the story forbids both directions. This is the
distinction the old three-state field could not draw, because `None` had to serve both for
"no landing owed" and "nobody has looked".

### What may create an assertion

Evidence is never derived from prose. `sprint/dag.py` consults a base commit whose *message*
closes an issue as a last-resort merge signal; that is appropriate for "should I re-run this
story" and inappropriate here — it is the signal that produced #2374. The observers here
accept, in order:

1. GitHub's merged-PR record, taking `mergeCommit.oid` as the landed commit. The only
   source that survives `merge_strategy: squash`.
2. Git topology, when the reviewed commit is provably reachable from the target branch: the
   first merge along the ancestry path, or the base tip for a fast-forward.

**A merged PR is evidence about this run only if it carried this run's work.** Branch names
are reused: `forge/issue-42` may carry a pull request that merged months ago, and a lookup
keyed on the branch alone returns it happily — producing an assertion that names *this*
run's reviewed commit beside *that* PR's merge commit, a landing fabricated out of two real
facts. So the PR must demonstrate containment: its head is an attested commit, or an
attested commit is in the list of commits it contains. The commit list is what makes this
survive a squash, which rewrites the head. With nothing attested to check against, the
lookup fails closed and the observation records `unknown` — a PR that cannot be tied to
this run is not evidence about this run.

Recency is not a tiebreak for correctness here, only among PRs that already passed
containment. A stale PR that merged *later* than the real one would otherwise win.

A PR reported merged whose merge commit cannot be named produces no assertion — the
observation records `unknown` and stops. `RECONCILABLE_OUTCOMES` (`queued`, `unknown`,
`timeout`) is the set a later pass will look at again; `refused`, `failed` and `closed` are
terminal, because a landing that was refused will not land itself and re-polling it forever
would turn reconciliation into a poll loop over the whole history.

### The observers

| Observer | Runs at | Covers |
| --- | --- | --- |
| `sprint.integration` | `_attempt_integration`, immediately after the landing resolves | Every `merge` and `merge-pr` landing attempt |
| `sprint.queued-pr` | `_resolve_queued_pr` during the work loop, and the sprint's queued-PR wrap-up | A queued auto-merge that resolves *before* the sprint exits, whichever of the two sees it |
| `sprint.batch-member` | `_apply_batch_landing_to_member` | A batch member, whose changes exist only on its leader's branch and land through the leader's carrier |
| `forge.reconcile` | `reconcile_landing_evidence`, called at sprint startup after the base-branch pull | Every landing that resolved *after* the sprint that requested it exited |

Two of these are one observer at two sites, and that is worth stating because it is where
this drifted. A queued pull request that merges, closes or fails while the sprint is still
running is resolved by `_resolve_queued_pr` and never reaches the wrap-up, so wiring the
wrap-up alone left every mid-loop resolution unpublished. `_resolve_queued_pr`'s own
docstring already listed durable landing evidence among the bookkeeping every caller owes —
the evidence was the one item not wired. `tests/test_landing_observation.py` now asserts
against the parsed module that every site which persists a landing also publishes evidence,
because the failure mode is a site being forgotten rather than a behaviour being wrong.

A batch member is a landing observation too. Its changes exist only on the leader's branch,
so the *leader's* merge info is threaded through as the carrier — the member has none of its
own — while its reviewed and gated commits are read off its own run, because the review and
gate that judged it were its own.

The reconciliation observer is the seam asynchronous modes needed and did not have. `sprint/prior_landing.py`
holds pure predicates over already-persisted mappings and has no project root, no GitHub
access and no publication capability, so it could not have been it. Reconciliation is driven
by the evidence tree — the handful of runs with an open attempt — rather than by the run
records, which accumulate forever and are almost all irrelevant to it.

## The matrix

`Record published` is the same answer in every row, which is the point: publication no
longer varies with how the work reached the branch. What varies is the evidence.

Transport per row is `direct` where the run lands in the project root and `memory-branch`
otherwise; the fallback makes `direct` degrade to `memory-branch` against a protected base.

### Synchronous landing (`on_approve: merge`)

| Outcome | Record published | Landing evidence |
| --- | --- | --- |
| Landed (fresh merge) | `direct`, falling back to `memory-branch` when the base refuses | Assertion. `carrier_kind: merge`, `carrier_ref` the base branch, `landed_commit` the base tip after the merge |
| Landed via gate-green rollback (#2028) | same | Assertion, `landed_commit` from the rollback record — the commit that actually reached the branch, not the checkpoint |
| Already-merged short-circuit | same | Assertion only if the merged carrier can be named; otherwise attempt `unknown`. The guard discards the worktree's commits, so a claim here would name work that did not land |
| Zero-delta (nothing ahead of base) | same | Attempt `failed`. Nothing landed |
| Landing refused (dirty root, unmet dependency, missing review) | same; record keeps `pending_integration` | Attempt `refused` / `failed` |
| Landing failed (merge conflict, hook rejection) | same; record keeps `pending_integration` | Attempt `failed` |
| Escalated / failed before any landing was attempted | same | **No evidence at all.** No landing was owed, so `landing_state` is `unresolved` and reconciliation never looks. Manufacturing an attempt would invent an obligation |

### Asynchronous landing (`on_approve: merge-pr`)

The two columns are the story's requirement that sprint exit be distinguished from what is
observed later.

| Outcome | At sprint exit | Later observed | Landing evidence |
| --- | --- | --- | --- |
| PR merged during the landing step | `merge-pr` reported merged | — | Assertion by `sprint.integration`, `carrier_kind: pull_request`, `carrier_ref` the PR number, `landed_commit` from `mergeCommit.oid` |
| Auto-merge queued, resolves before sprint exit | queued, then polled MERGED — by the work loop's resolver or by the wrap-up, whichever sees it first | — | Attempt `queued`, then assertion by `sprint.queued-pr` |
| Auto-merge queued, still open at exit | attempt `queued`, then attempt `timeout` when forge stops waiting; record keeps `pending_integration` | PR merges later | Assertion by `forge.reconcile` on the next sprint. The run record is **not** rewritten |
| Auto-merge queued, PR closed unmerged | attempt `queued` | PR closed | Attempt `closed`. Terminal; no assertion, ever |
| Required checks decided red | attempt `failed`; record keeps `pending_integration` | — | Attempt `failed`. Terminal |
| PR merged but the merge commit cannot be named, or the only merged PR on the branch did not carry this run's commits | — | — | Attempt `unknown`. Reconcilable: a later pass that can name and verify the carrier may still assert |
| Landing failed before a PR existed | attempt `failed` | — | Attempt `failed` |
| Escalated / failed before any landing | no evidence | — | `unresolved` |

### Pull-request-only (`on_approve: pr`)

Forge creates the PR in `coordinator/completion.py` and never queues it for the sprint's
poller — the sprint has no further part in it.

| Outcome | At sprint exit | Later observed | Landing evidence |
| --- | --- | --- | --- |
| PR created, unresolved | attempt `queued` where a landing step ran; otherwise no evidence | PR merges | Assertion by `forge.reconcile` |
| PR created, never merged | attempt `queued` / none | still open | Stays `unresolved`. Never `not_landed`, because nobody decided anything |
| PR closed unmerged | attempt `queued` / none | closed | `forge.reconcile` records `unknown` (it observes no merged PR); the outcome remains unresolved rather than being asserted either way |
| Escalated / failed | no evidence | — | `unresolved` |

> **Known gap.** A `pr`-mode run that never enters a landing step leaves no attempt, and
> reconciliation is attempt-driven, so its later merge is not picked up. Closing this means
> emitting a `queued` attempt at PR creation in `coordinator/completion.py`. It is a small,
> well-scoped follow-on and is listed below; the PoC exercises the reconciliation path
> through `merge-pr`, where the attempt does exist.

### No landing (`on_approve: none`)

| Outcome | Record published | Landing evidence |
| --- | --- | --- |
| Any | `memory-branch` | **None, by design.** No landing was owed. `landing_state` is `unresolved`, which is the honest answer to "did it land?" for work that was never asked to land |

### Every other outcome

Any outcome not enumerated above — including ones that do not exist yet — leaves the run
record unmodified and produces no positive landing assertion, because an assertion can only
be constructed by naming a landing that was observed. The failure direction is closed at the
constructor, not at the call sites.

## Seams touched

| Seam | Change |
| --- | --- |
| `coordinator/landing_evidence.py` (new) | Artifact builders, validators and the stdlib-only store. Owns the read locations, including the staging read-through |
| `sprint/memory_publication.py` (new) | Staging and the memory-branch/PR transport. Does not import the runner (ADR-0008) |
| `sprint/landing_observation.py` (new) | When an assertion may be built; the in-sprint and post-exit observers |
| `sprint/audit_publish.py` | Transport selection, the policy-refusal fallback, the third tracked memory tree |
| `sprint/runner.py` | Evidence emission at `_attempt_integration` and the queued-PR wrap-up; reconciliation at sprint startup; mid-sprint publish no longer requires quiescence for runs that do not land locally; a drain immediately before each admission |
| `sprint/audit.py`, `coordinator/landing_evidence.py` | Atomic-replace temporary files moved out of the tracked memory trees into `.forge/audits/.tmp/` |
| `sprint/audit.py` | A canonical run record never carries a resolved-negative landing claim, on creation or replacement; the "already written" check reads through to the staging area, because the transport drains the canonical tree to publish it |
| `coordinator/audit_storage.py` | The evidence tree joins `AUDIT_PATH_REGISTRY`, so the diagnose briefing renders it |
| `cli/init_commands.py`, repo `.gitignore` / `.gitattributes` | `.forge/audits/landing/` re-included as project memory and marked generated |

Two seams were touched *because of* this work rather than by it:

* `sprint/audit_publish._porcelain_paths` sliced a fixed two-character status column from
  text `_run_shell` had already stripped, so the first path was mis-parsed whenever its
  status was worktree-only (`" M "`). Sibling-artifact attribution then failed to recognise
  forge's own artifact and the landing refused instead of republishing. Both parsers now
  share one strip-tolerant implementation.
* The mid-sprint publish's quiescence requirement was attributed to publishing; it belongs
  to project-root landing. Re-keying it is what lets a parallel `merge-pr` sprint publish
  between a completed story and its successor's entry.
* Both canonical writers put their atomic-replace temporary file inside the tracked tree
  they were writing to, making a write in progress indistinguishable from project memory:
  transient dirt in the shared checkout, and a publishable half-written artifact. Found by
  this spike's own proof of concept, which saw `?? .forge/audits/runs/<run>.tmp` at a
  story's admission.

**Not changed:** `CURRENT_RECORD_SCHEMA_VERSION` and `SUBSTRATE_SCHEMA_VERSION`. The run
record's serialized shape is untouched — evidence is a *new artifact beside it*, not a new
field in it — so neither the schema guard nor `rebuild_from_runs` has anything to migrate.
An indexed projection of evidence into the substrate would need both, and is a follow-on.
(Shipped since, as #2849: `SUBSTRATE_SCHEMA_VERSION` 13. `CURRENT_RECORD_SCHEMA_VERSION`
stayed put — the projection is derived from the artifacts, not from a record field.)

## Testing

| Property | Test |
| --- | --- |
| An attempt has no spelling of "landed"; no attempt outcome satisfies a landed query | `tests/test_landing_evidence.py` |
| An assertion cannot omit what it claims; reviewed and landed commits are named separately | `tests/test_landing_evidence.py` |
| Assertions are write-once; attempts accumulate; a malformed artifact reads as unresolved | `tests/test_landing_evidence.py` |
| No evidence is `unresolved`, not landed and not failed | `tests/test_landing_evidence.py` |
| The protection hook really refuses a direct base commit (fixture guard) | `tests/test_memory_publication.py` |
| Staging leaves the checkout clean, including artifacts a refused commit left in the index | `tests/test_memory_publication.py` |
| A failed publish retains staged memory rather than losing it | `tests/test_memory_publication.py` |
| Memory reaches the remote with the base branch untouched; a fresh clone carries the corpus | `tests/test_memory_publication.py` |
| Neither writer leaves a temporary file in a tracked tree; a stray one is never publishable | `tests/test_memory_publication.py` |
| A stale merged PR on a reused branch is refused as the carrier; a squashed PR is still recognised by its commit list; an unverifiable lookup fails closed | `tests/test_landing_observation.py` |
| A queued PR that merges, closes or times out *during the work loop* publishes its assertion or terminal attempt, not just one resolved at wrap-up | `tests/test_landing_observation.py` |
| Every site that persists a landing also publishes evidence for it (asserted against the parsed module, so a forgotten site fails a test) | `tests/test_landing_observation.py` |
| A failed or timed-out landing does not put a negative claim in the run record, on rewrite or on first write; a successful one may still advance it | `tests/test_landing_observation.py` |
| Every story admission is preceded by a drain, and a sibling finishing in that window does not dirty the newcomer's checkout | `tests/test_protected_base_publication_poc.py::test_every_admission_is_preceded_by_a_drain_of_sibling_memory` |
| Sprints accumulate onto one memory branch; the branch restarts from base once merged | `tests/test_memory_publication.py` |
| The direct transport still commits, pushes, reconciles and raises exactly as before | `tests/test_sprint_audit_publish.py`, `tests/test_sprint_parallel.py::TestSprintRunAuditCommit` |
| A story's artifacts still do not refuse its successor under the sequential seam | `tests/test_sprint_run_artifact_publish_timing.py` (unchanged) |
| End to end: protected base, `--parallel 2`, `merge-pr`, fresh clone | `tests/test_protected_base_publication_poc.py` |

## Proof-of-concept result

`test_protected_base_parallel_merge_pr_poc` runs the mandated configuration: three
independent stories, `--parallel 2`, `on_approve: merge-pr`, a bare origin with a
`pre-receive` hook that refuses any non-merge commit on the base branch. Story A lands
during the sprint, story B is queued and merges after the sprint exits, story C's landing
fails. It establishes:

* **No story was refused.** Story C was admitted while story B was still in flight, with
  story A's artifacts already produced and integrated, and the landing precondition
  evaluated at its entry was clean. The scenario is *sequenced* rather than raced — story A
  finishes only once story B is running, and story B stays running until story C has been
  admitted — so the condition the acceptance criterion names is reached on every run rather
  than when the scheduler happens to interleave that way.
* **The protected branch received no direct commit.** Its first-parent history across the
  run is exactly `["Merge PR for story-a"]`.
* **No outcome without a verified landing touched its record.** Story B's landing timed out
  and story C's failed outright; both records still read `pending_integration`, asserted as
  that value rather than merely as "not landed" — a record stamped `failed` would satisfy
  "not landed" while being exactly the negative claim the model forbids.
* **A stale pull request on a reused branch is refused.** Story A's branch also carries an
  older merged PR whose commits are unrelated to this run. The assertion names `#1`, the PR
  that carried the reviewed commit, not `#0`.
* **A fresh clone** of the memory branch holds one run record for each of the three
  completed stories, and positive landing assertions for exactly story A and story B —
  each naming the run, the reviewed and gated source commit, the target branch, the pull
  request, the landed commit and the landing mode. Story C has attempt records and no
  assertion, and its record carries no landed claim.
* **The later observation added an artifact rather than rewriting one.** Story B's run
  record is what the sprint published; `forge.reconcile` published the assertion beside it.

### Prevented, or unreachable?

**In the mandated configuration, no refusal was reachable — the result is
"unreachable", not "prevented".** `sprint/runner.py` sets `config_lands_in_project_root`
false for `on_approve: merge-pr`, so no story's entry evaluates the landing precondition at
all. The PoC measures the precondition itself, which is why it can say the *checkout* was
clean; but a checkout nothing was going to check is not evidence that the seam prevents a
refusal. The story asks for this distinction explicitly and only the first finding
establishes that the seam is sound.

`test_a_reachable_refusal_is_prevented_by_the_publication_seam` supplies the second half.
Under `on_approve: merge` the precondition *is* evaluated at every story's entry, and
against a base branch that refuses forge's memory commit — a `pre-commit` hook rejecting
`.forge/` changes, which is the reported policy at the point a local-merge run meets it —
the refusal is genuinely reachable. The test asserts both halves in one run:

* **Counterfactual** (fallback disabled, direct transport only): story B is refused, and
  the refusal names `.forge/audits/runs`. This is the reported bug, one step downstream.
* **With the seam**: story B is not refused. The commit is still refused by the hook; the
  fallback drains the checkout and publishes from the memory branch.

So: the mandated PoC establishes the *transport* (memory reaches a protected repository,
under parallelism, with correct evidence), and the companion test establishes the *seam*
(a real, reachable refusal is prevented). Neither alone was sufficient.

## What a project must do to adopt this

1. **Allow forge to create pull requests** — a `forge/project-memory` branch and one PR into
   the base branch. This replaces the direct-commit allowlist entry; that entry should be
   removed, which is the point of the change.
2. **Merge the memory pull request.** Forge will not merge it. Until it merges, the corpus
   is on the memory branch and a fresh clone of the base branch does not have it.
3. **Regenerate the git policy block** so `.forge/audits/landing/` is tracked
   (`forge init` writes it; the block is idempotent). Without the re-include, git never sees
   landing evidence as pending and it is never published.
4. **Nothing else.** No config change is required — the transport is derived. `on_approve`,
   `auto_push` and `merge_strategy` keep their meanings.

Projects on an unprotected base branch see no behaviour change for `merge`/`--auto-merge`
runs: those still commit directly. `pr`, `none` and `merge-pr` runs move to the memory
branch, which removes an audit commit that used to appear on the local base branch and
contaminate story-branch diffs.

## Adoption decision

**Build.**

The defect is not a degradation with a workaround. Under a protected base a project either
relaxes its branch policy for forge's benefit or forfeits the accumulating corpus, which is
the feature; the workaround in the field is a privilege escalation. Retry cannot help,
because the operation forge retries is the one the policy refuses.

The landing-evidence half is not a nice-to-have bundled with it. The publication path
determines what the presence of a record means, and a model that publishes records for runs
that did not land *must* carry landing evidence separately or it silently asserts landings
that did not happen. The two have to be decided together, and they have been.

The seam is established rather than assumed: the reachable-refusal test shows prevention,
not merely absence, and the honest reading of the mandated PoC is recorded above rather
than presented as if it established more than it does.

The cost is bounded. The record shape does not change, so no schema version moves and no
substrate rebuild is implied. The direct transport is preserved intact for the runs that
were already using it safely, and every new failure mode degrades to "staged locally,
retried next sprint" rather than to lost records or a contaminated checkout.

## Follow-ons

1. **Stuck memory PR signal.** Warn when the memory pull request has been open across N
   sprints. Today the operator sees only the recorded publish end state and the PR itself.
   The PoC resolves the PR immediately and does not exercise this.
2. **`pr`-mode attempt at PR creation.** Emit a `queued` attempt in
   `coordinator/completion.py` when the PR is created, so a `pr`-mode run that never enters
   a landing step is still reconcilable. (Matrix gap, noted above.)
3. **Evidence projection into the substrate.** *Shipped — #2849, `SUBSTRATE_SCHEMA_VERSION`
   13.* `landing_assertions` and `landing_attempts` index the evidence tree one row per
   artifact, refreshed in place by `audit_storage.sync_landing_evidence` on every writable
   open and rebuilt from the same artifacts by `rebuild_from_runs`. The landed query
   (`has_review_approve_in_substrate(require_landed=True)`) now filters on the projected
   assertion, and `audit_read_model.landing_states` reports `landed` / `not_landed` /
   `unresolved` with each assertion's own `observed_at`.

   **Operator consequence.** A run that carries `landing_status='landed'` but no published
   assertion now reads as *unresolved*, which is what the field always meant and could not
   say. Resume merged-detection (`sprint/dag.py:_has_prior_review_approve`) is the consumer
   that notices: a story whose landing predates evidence will not be recognised as merged
   until reconciliation publishes an assertion for it. Reconciliation runs at sprint
   startup, so this closes itself over a sprint; a repo with a long pre-evidence history
   should expect one pass of it.
4. **Re-point the flattened readers.** `cli/audits.py`, `cli/sprint_digest.py` and
   `coordinator/issue_cost.py` read `landing_status` directly. They keep working because the
   field keeps being populated; they should move to `landing_state` once (3) exists, so that
   "unresolved" becomes visible to an operator rather than collapsing into "not landed".
5. **A `forge` command for reconciliation.** It runs at sprint startup today, which covers
   the compounding case. An explicit command would let an operator close out evidence
   without starting a sprint.
