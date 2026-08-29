# Controller Runbook — Operating TheForge

This is the operational cheat-sheet for a **controller session**: the operator's
right-hand for running and diagnosing sprints, cutting release candidates, and
filing issues from sprint failures. Read it before you run anything.

It complements — does not restate — two other docs:

- **`CONVENTIONS.md`** holds the *doctrine* (dogfood floor, forward-port model,
  what goes in repo vs. operator memory). Cross-links below point into it.
- **`docs/guides/cli-reference.md`** holds the *full* flag reference. This
  runbook only calls out the operationally load-bearing flags and the traps.

If a fact keeps getting re-learned across sessions, it belongs **here** (or in
`CONVENTIONS.md`), not in one AI's private memory.

---

## 1. Running a sprint

```bash
forge sprint --verbose --issues N,N,N --budget 50 --parallel 3
```

- `--resume` re-enters an existing run. **Always `--resume`** rather than
  deleting worktrees and re-running fresh (unless explicitly asked to start
  clean).
- **`--base-branch <branch>`** overrides `workspace.base_branch` for this run
  without editing `forge.yaml`. Set it to the branch fixes should land on. When
  dogfooding a release line (checkout on `release/vX.Y`), pass
  `--base-branch release/vX.Y` — otherwise fixes land on `main`, and worse (see
  §3).
- **Confirm with the operator per-invocation before launching.** Sprints spend
  money; "fix it" is not authorization to spend.

### PRESERVED — escalated worktree held for re-entry

A sprint can report a story as **PRESERVED** when launch triage finds an
existing escalated worktree from a prior generation and deliberately declines to
reschedule it in this run. On `forge status` the row stays `status=preserved`
with phase `—`; in the run log and launch-time stderr the story is reported as:

Issue-backed story:

```text
escalated worktree preserved for human review; resolve with `forge review --issue 2475`
```

File-backed story:

```text
escalated worktree preserved for human review; resolve with `forge review <story-file>`
```

Treat this as a recoverable halt, not a silent drop. The worktree and resume
record are still on disk; the exit is the command named in the report:
`forge review --issue 2475` for issue-backed stories, or
`forge review <story-file>` for file-backed stories. For file-backed reports,
the runtime substitutes the concrete path when it already knows it; the
placeholder above is schematic. `forge review` triages the existing worktree
and resumes from the correct phase (REVIEW, DEV, or full pipeline) instead of
starting over.

### Auth readiness gate (issue #1952)

Before any story is dispatched, a sprint inspects the credential each
CLI-backed Claude profile will present (`~/.claude/.credentials.json`) —
including the PLAN and PLAN_REVIEW agents when those phases are enabled. If it
holds no usable token, the sprint aborts in seconds with
`SprintAuthUnavailable`, names the credential path, and marks **no** story
failed — nothing about the work was judged.

The credential the `claude` path uses is in the **same OAuth family as an
interactive Claude Code session**. A new interactive sign-in revokes the
previous family server-side, which silently disables the substrate. The
signature is distinctive: `accessToken` and `refreshToken` blanked to empty
strings while `refreshTokenExpiresAt` stays a future date, so the file reads as
valid until token *length* is checked. Remedy: mint a fresh long-lived token
with `claude setup-token` and store it in `.forge/.env`
(`CLAUDE_CODE_OAUTH_TOKEN`) rather than re-running interactive `/login` — an
interactive sign-in revokes the previous family and just points the same
conflict the other way. Same guidance as the auth section of the
[troubleshooting guide](troubleshooting.md).

If the credential is revoked *mid-sprint*, a circuit breaker trips on the first
fatal auth failure: in-flight workers are stopped and no further story is
dispatched, rather than re-presenting the same rejected credential once per
story and per phase. Stories the breaker cancels are recorded **SKIPPED** and
attributed to the credential, not FAILED — the sprint killed them, no model
judged them, and they contribute nothing to adaptive memory.

### Landing precondition — clean project root (issue #2048)

Under a landing workflow (`workspace.on_approve: merge`, or `--auto-merge`), a
sprint refuses at entry when the project root has uncommitted changes, naming
the offending paths:

```
LANDING PRECONDITION: uncommitted changes in project root /path/to/repo:  M forge.yaml.
```

The condition is the same one `_merge_branch` enforces at landing — untracked
files included — moved to the point where it costs nothing. Before this check,
a modified `forge.yaml` in the root let a story run dev and review to approval
and then fail with `MERGE_FAILED`, leaving reviewed work stranded on its feature
branch after the full spend. Remedy: commit, stash, or revert the change in the
project root, then re-run. Each story re-evaluates the condition at its own
entry, so dirtying the root mid-sprint stops the *next* story rather than
costing it — that story escalates in WORKSPACE with the message above instead
of running.

Whether a story lands locally is decided per story, not per config value. A
**dependency parent carried by this sprint** is merged locally to unblock its
child, so those parents carry the precondition even under `on_approve: none` or
`pr` — in sequential mode the parent is eager-merged, and in parallel mode the
scheduler forces the merge after it returns. Two exceptions follow from how that
resolution actually works, and both mean *no* refusal:

- **`on_approve: merge-pr` in parallel mode.** The parent already has its own
  landing, so the scheduler never rewrites it to a local merge; it lands through
  its PR. Sequential mode still eager-merges it, so there the precondition
  applies.
- **`--auto-merge` in parallel mode.** The flag is dropped when
  `max_parallel > 1`, so it cannot cause a local merge and the configured
  landing path stands.

Because of that, the sprint checks twice, and the log line names which pass
refused:

- `[sprint-entry]` — the configuration-level answer (`on_approve: merge` or
  `--auto-merge`). Knowable immediately, so this one costs seconds.
- `[dependency-resolved]` — the dependency-derived answer, asserted once the
  satisfied and resume-triage sets say which parents will actually be
  dispatched. Still ahead of intake remediation, batch preflight, and every
  story dispatch, so nothing has been spent.

A `depends_on` edge imposes nothing when it points at an **external** issue (not
carried by this sprint) or at a parent already **satisfied** — merged into the
base branch, or triaged `skip_merged` on resume. Those parents never run and
never merge, so a PR-landing sprint whose only edges are of that kind runs
against a dirty root untouched. Workflows where nothing merges into the local
checkout are genuinely unaffected.

### Worktree provenance — the story text changed under an existing tree (issue #2288)

When a run finds an existing worktree for a story, it now asks the same question
the resume record already asks about phase records: was what is in there
produced against the story text this run is executing? Editing an issue body
while its story is stopped makes the answer no, and the log says so:

```
↻ WORKSPACE  reusing existing worktree: .../issue-2284
⚠ WORKSPACE  story text changed since this worktree's contents were produced
  produced against story 4f3c1ab90d2e, now running story 9b71c0de4415
```

**The work is kept, never deleted** — a stopped story's tree is usually exactly
what should continue, and discarding it on a wording change would be worse. What
changes is that the fact is stated: the dev agent's first prompt carries an
*Inherited Working Tree* section telling it to inspect what is already there and
say in its handoff what it kept and what it discarded, and the run audit records
`workspace.story_provenance` (`fresh_worktree`, `story_content_match`,
`story_content_changed`, or `unknown`) plus `workspace.inherited_superseded_work`.

Operator call when you see the warning: continuing is fine if the edit was a
clarification; if you rewrote the diagnosis, remove the worktree
(`git worktree remove --force <path>` plus `git branch -D feat/<slug>`) before
re-running so the dev starts clean. `unknown` just means the tree predates the
record — it is not a defect.

### Which attempt a resume proceeds from (issue #2351)

The per-story resume record (`.forge/resume_state/<slug>.json`) keeps the
**best-founded** block for each phase, not the most recent one. A preflight
attempt that was killed by signal — `degraded: true`, `criteria_checked: []` —
no longer displaces a stored block from an attempt that exited cleanly, so
resuming continues from the clean result instead of deterministically replaying
the failure and the higher complexity score it derived while observing nothing.

Two signals decide it, in order: whether the attempt was marked degraded, then
how much it examined (`criteria_checked` for preflight). `routing_decision` and
`complexity_routing_audit` inherit the founding preflight's standing via the
`complexity_source` / `preflight_degraded` provenance already stamped on the
audit. Blocks that record a *decision* rather than an observation
(`plan_review`, `escalation`, `timeout_escalation`, `review_progress`) carry no
foundedness signal and still merge latest-wins.

Every selection is recorded, so this never has to be reconstructed by diffing
run directories:

- `resume_selection` on the record itself — a bounded list of
  `{phase, action, reason, prior_foundation, incoming_foundation, prior_run_id,
  incoming_run_id}`, where `action` is `kept_existing` (a less-founded save was
  refused) or `replaced_existing` (a better-founded save displaced what was
  there).
- `phase_recovery.block_selection` in the run audit and in the `phase_recovery`
  log event — the same list, at the moment a resume reads it.

Where a degraded attempt's derived values are the **only** ones available, they
are still carried forward — a resumed story needs a number — but marked:
`state.complexity_routing_audit` and `story_allocation` carry `unfounded: true`,
`unfounded_reason` (the degraded reason), and `unfounded_source`. Those markers
survive preflight's re-scale and the allocation evaluation, so an allocation or
worker ceiling resting on a phase that observed nothing reads as such on the
story row rather than as a derived figure.

### PREFLIGHT complexity gate — approve the scope or send it back (issue #2681)

Preflight sizes a story for cents; everything after it is where the money goes.
A story whose preflight verdict is PROCEED and whose complexity score reaches
`retry.preflight_complexity_gate_threshold` (shipped: 9) now stops at the end of
PREFLIGHT and asks, before planning, dev, or review is charged:

```
▸ #2541  PREFLIGHT  complexity 9 (impl 9, validation 3)  — awaiting operator
  Threshold 9. Nothing has been spent beyond preflight for this story.

    forge decide 7c1e04b9d3af approve      plan and implement it as scoped
    forge decide 7c1e04b9d3af decompose    return it to be split
```

The decision is keyed by the **story's** run id — the one `forge status` shows on
the story row, not the sprint's. Other stories keep running while one waits.

- **approve** — the run continues exactly as it would have without the gate, and
  the audit records that the story was explicitly approved at that score. A
  large-but-cohesive story is never blocked by its size alone.
- **decompose** — the run ends having spent only preflight. The story is
  reported **returned for decomposition** (`outcome: decomposed`, `⤺` on the
  sprint row), which is not a failure and is not an escalation.
- **no answer** — the run takes `retry.preflight_complexity_gate_no_decision`
  (shipped: `decompose`), and the audit says no operator decision was recorded.
  That key accepts only `approve` or `decompose`; anything else returns the
  story, so a typo cannot spend on an unapproved one.

The gate is anchored to the end of preflight rather than to PLAN, so a story
preflight classified `implementation_ready` — which skips planning — still stops
here. There is no enable switch: raise the threshold above 10 to disable it.
`--until preflight` still stops at preflight without opening the gate.

Where to look afterwards: `preflight_complexity_gate` in the run audit (whether
it opened, both complexity axes, the threshold, the decision and its source) and
`outcome.returned_for_decomposition`.

### SPEC_GAP — the dev agent is asking, not guessing (issue #2122)

A dev agent that reaches an acceptance criterion which does not define the case
in front of it can now stop and ask, instead of guessing and letting the guess
become the spec. `forge status` shows the pause as an ordinary pending decision
with `[SPEC_GAP]`; the reason text names the criterion, the undefined case, the
options the agent weighed, and the assumption that takes effect if you say
nothing.

Unlike every other gate, this one takes **free-form text**, not a menu:

```
forge decide <run-id> "leave unmarked; do not attach an uncorrelated session"
```

What happens either way:

- **You answer.** The answer is injected verbatim into the next dev prompt as a
  decision (not a suggestion) and recorded in the audit. The run re-enters DEV
  directly — **no review cycle is spent on the gap**.
- **You do not answer**, or the run has already spent its allowance
  (`retry.max_spec_gap_pauses`, default 1). The run proceeds under the
  assumption the agent recorded, and the audit says which of the two happened
  (`source: no_answer` vs `source: allowance_exhausted`). No gap pause blocks a
  run indefinitely, and none is discarded without a record.

Where to look afterwards: `spec_gaps.events` and `spec_gaps.resolutions` in the
run audit. `resolutions` also persists to
`.forge/resume_state/<slug>.json`, so a resume — or a later fresh run of the
same story — starts with the answer already in the dev context rather than
re-asking. Change the story text and the stored answers no longer apply: they
settle a criterion, and that criterion may no longer be the one in the spec.

An answer here is worth roughly one question's latency. The failure it replaces
(hdp#253) was 3 review cycles, 0/2 APPROVE, $8.80, and nothing merged.

## 2. Branch & forward-port model

- Fixes land on the **base branch**. `forward-port.yml` then auto-carries every
  `release/*` merge into `main` as a real merge-commit PR (GitHub App token,
  auto-merge on green CI). `main` stays a strict **superset** of every release
  line.
- **Never commit directly to `main`.** All changes go through a
  branch/worktree + PR.
- After any *manual* PR intervention, re-arm auto-merge — a force-push clears it:
  ```bash
  gh pr merge <N> --auto --squash --delete-branch
  ```

## 3. The `base_branch` trap (issue #1928, fixed) — historical note

`workspace.base_branch` defaults to `main`. After every merged PR, forge's
"best-effort local cleanup" (`coordinator/completion.py`) fast-forwards the
**project-root checkout** to `origin/<base_branch>`. Before #1928 (closed
2026-07-25) it did so unconditionally: sitting on a release branch while
`base_branch` was still `main` silently dragged the release branch up to
`origin/main` — main's dev version, its `[Unreleased]` changelog, its
forward-port merge commits — with no error (failures were swallowed as
"non-fatal").

Fixed by #1928: `_step_cleanup` now checks the checked-out branch first and
**skips** the fast-forward when it doesn't match the configured base, logging
`skipped local base branch sync after merge because checked-out branch … does
not match configured base …`; fetch/merge failures warn instead of being
swallowed.

Standing hygiene: keep `--base-branch` matching the branch you have checked
out. Forge now refuses sprint launch and end-of-sprint audit publish when the
project-root checkout and configured base branch differ, and still skips the
post-merge local sync if you somehow hit the mismatch in older runs. PR targets
and the collision DAG still follow the configured base. Symptom of the pre-fix
corruption, if auditing old history: `git show
release/vX.Y:pyproject.toml` reads main's dev version instead of the branch's
`rc` version.

## 4. Cutting a release candidate

```bash
scripts/cut-rc.sh X.Y.Z        # RC number computed from origin's vX.Y.Zrc* tags
scripts/cut-rc.sh X.Y.Z n      # explicit override
```

- With no `RC_NUM` argument, cuts one past the highest `vX.Y.ZrcN` tag on
  origin (`rc0` if there are none), and prints which number it picked and why.
  No `git tag --list` lookup needed before a repeat cut.
- Reads the current version on the release branch, **overwrites** it to
  `X.Y.Zrcn`, runs the gate, commits, tags, pushes, and configures branch
  protection so auto-merge works. A stale version on the branch is therefore
  harmless — cut-rc overwrites it.
- `cut-rc.sh` only `pull --ff-only`s the *release* branch and creates it from
  `main` when it doesn't exist yet; it **never** merges `main` into an existing
  release branch. (So cut-rc is never the cause of a release branch picking up
  main's history — see §3 for what was, pre-#1928.)
- `--resume` skips bump/commit/tag/push when the tag already exists on origin.
- **Floor doctrine:** cut when the release is "solid enough to be the dev
  substrate for the next release," not when every issue is closed. Non-floor
  bugs roll to the next minor. See `CONVENTIONS.md` → dogfood floor.

## 5. Merge gate

- **PASS gate + review APPROVE before anything merges to `main`** — even when
  the operator explicitly asks to merge. Surface the gate/review state, then
  merge.

### End-of-sprint audit publish (issue #2006)

The last thing a sprint does is commit `.forge/audits/runs/*` in the project-root
checkout and push the base branch. A push rejected because the base branch moved
— usually the merge of this sprint's *own* story PR — is reconciled (`git fetch`
+ `git rebase origin/<base>`) and retried up to three times before the sprint
fails.

If you find audit commits sitting local, read
`.forge/audit-publish-state.json` in the project root rather than guessing which
of the failure modes you hit — the `state` field says which:

| `state` | Meaning | Remedy |
| --- | --- | --- |
| `published` | Commit reached `origin/<base>`. | none |
| `clean` | Nothing was pending this run. | none |
| `local_only` | Publish deliberately skipped (`auto_push` off on a locally-landing run). | push `<base>` yourself before any run that diffs against `origin/<base>` |
| `committed_unpublished` | The run died between the commit and the push. | fetch, rebase, push |
| `branch_mismatch` | Publish refused because the project-root checkout was on a different branch than `<base>`. | check out `<base>` and rerun publish, or move the pending audit records off the unrelated branch first |
| `push_refused` | Remote refused the push through all retries; `detail` has git's output. | inspect the remote, then fetch/rebase/push |
| `reconcile_failed` | The fetch or rebase itself failed (e.g. conflicting audit records); any partial rebase was aborted. | resolve by hand |
| `verify_failed` | Push reported success but `<base>` is still ahead. | fetch, rebase, push |

A *stale* file (state `published`/`clean`) next to unpushed audit commits means
the run ended before it ever reached the publish step.

## 6. Dogfood substrate model

- Two runtimes that must stay **disjoint**: the *orchestrator* is the released
  `rc` tag in an isolated venv (plain `forge`), pinned; the *patient* is
  `main`/the working tree with its own deps. Don't editable-install main as the
  orchestrator, and don't cherry-pick schema changes between them.
- Pin dogfood to the **latest tag**; re-cut `rcN` as fixes land or the lag
  returns one version up. See `CONVENTIONS.md` → dogfooding config.

## 7. Filing issues from sprint failures

- **Bugs:** observed behavior + expected behavior only. `EXPECTED` must state a
  **category-level rule that generalizes** beyond the trigger, written as
  flowing prose — no bulleted rule-lists (the preflight parses those as feature
  ACs), no provider/model names, no "Acceptance is…" section. Add a
  **Diagnosis** section whenever you have an RCA: observed symptom, evidence
  (with `file:line`), ruled-out hypotheses, confirmed cause, affected code path,
  fix-success criterion. Symptom bugs don't enter a sprint without that
  diagnosis, and reviewers verify **symptom resolution**, not just
  implementation correctness.
- **Features / enhancements:** need an `## Acceptance criteria` section and a
  concrete example (sample output / before-after). Body = WHAT/WHY, not HOW.
- **Label and milestone at creation** (`gh issue create --label bug --milestone
  vX.Y.Z`) or the shape gate / triage skips it. When reporting a filed issue,
  include full intake state: labels, milestone, sprint-gate readiness.
- **A refusal that cites a standing policy names its provenance class.** If a
  run surfaces a retraction or ratification candidate, decide which it is —
  ratify it with a durable reference, or retract it — per
  [policy assertion provenance](policy-provenance.md). Unmarked policy prose is
  advisory and cannot block chartered work.

## 8. Deciding what to work on

Which command answers which operator question. The last row is the important one:
**no tool decides priority.** That is a standing condition, not a missing feature —
see [ADR-0010](../adr/0010-triage-disposition-shelved.md).

| Question | Command | Limitation |
|---|---|---|
| What is open in the milestone? | `gh issue list --milestone vX.Y.Z --state open --limit 500` | No Forge equivalent. **`--limit` defaults to 30 and truncates silently** — always pass an explicit limit |
| What is ready to sprint? | `forge status --ready --milestone vX.Y.Z` | Shows only issues already `ready` |
| Is this finding still active, and where is the real seam? | `forge diagnose --dry-run --issue A,B --parallel N` | Spends (~$0.5/finding). Read the structured premise-verification fields (`already_resolved`, `absent_premises`, `unchecked_premises`) alongside the narrative |
| Persist that diagnosis into the issue | same, without `--dry-run` | Mutates issue bodies |
| Make an issue structurally runnable | `forge shape`, `forge groom`, `forge diagnose` | Readiness only, never priority |
| Verify a sprint before spending | `forge sprint --dry-run --issues … --budget …` | Requires the work already selected |
| **fix now / fix later / other milestone / close?** | **none — operator decision** | `forge diagnose` supplies evidence for the call; it does not make it |

`forge triage` does **not** answer the last row. Its disposition proposals are shelved
(ADR-0010): a full pass returned 29 `needs_verification` out of 30 findings because the
proposer's evidence is artifact presence and churn, which cannot establish whether a
behavioral claim still holds. Interactive report generation, `--ratify` and `--discard`
still work; the proposal and headless paths are shelved. Do not re-run triage on a
backlog expecting dispositions.

To groom a batch of findings, diagnose them and read the results:

```bash
forge diagnose --verbose --dry-run --parallel 2 --issue 2660,2678
```

Closed issues are refused, by design.

---

## Where knowledge goes

| Kind of fact | Home |
|---|---|
| How the tool works — commands, flags, branch model, traps, procedures | **this runbook** |
| Project doctrine, conventions, architecture, dogfood policy | **`CONVENTIONS.md`** |
| Operator-personal working style — confirm-before-spend, timezone, tone | **AI user memory** (not the repo) |

Repo docs serve every AI (Claude, Codex, Gemini) because each one's context file
(`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) redirects to `AGENTS.md`, which points
here. A fact that lives only in one AI's private memory does not survive a switch
to another AI — or often to the next session. Put durable operational knowledge
here.
