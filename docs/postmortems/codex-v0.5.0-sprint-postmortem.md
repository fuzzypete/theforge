# v0.5.0 Sprint Postmortem

## Summary

The last completed `v0.5.0` sprint run started at `2026-04-05T08:12:06Z` and
finished at `2026-04-05T09:23:42Z`. It recorded 13 stories, 6 successes, 7
failures, and `$40.5313` in spend in
[`sprint-summary.yaml`](../../.forge/logs/v0.5.0/sprint-summary.yaml) and
[`sprint-audit.yaml`](../../.forge/audits/sprint-audit.yaml).

The dominant failure modes were:

- release artifacts were not self-consistent because multiple sprint restarts
  were appended into the same `run.log` while the summary files only reflected
  the final rerun
- approved stories still escalated during merge and PR completion because the
  pipeline was too optimistic about branch protection, rebases against a moving
  `main`, and zero-delta branches
- two expensive stories (`issue-322`, `issue-366`) burned retries because the
  dev loop accepted patch-like or self-reported outputs without first proving a
  real code change landed in the worktree
- multiple stories failed on hard conventions, especially `max_module_lines`,
  after three retries without a targeted remediation path
- preflight remained too permissive when the agent returned malformed output or
  failed entirely, allowing ambiguous work to proceed into more expensive phases

In short: the biggest problems were not only story-level bugs, but weak run
artifact hygiene, permissive phase transitions, and completion logic that did
not adequately validate branch state before attempting merge automation.

## Detailed Findings

### 1. Run Artifacts Were Not a Single Coherent Sprint Record

The `v0.5.0` release folder does not represent one clean sprint attempt.
[`run.log`](../../.forge/logs/v0.5.0/run.log) contains four separate
`Detached sprint starting` blocks, while
[`sprint-summary.yaml`](../../.forge/logs/v0.5.0/sprint-summary.yaml) and
[`sprint-audit.yaml`](../../.forge/audits/sprint-audit.yaml) describe only the
final 13-story rerun.

That mismatch explains why:

- `run.log` shows 18 stories, then 16, then 13
- the `v0.5.0` log directory contains per-story artifacts from earlier reruns
- the top-level summary reports only the last run state

This is the largest auditability issue from the sprint because it makes
postmortem analysis depend on timestamp reconstruction instead of a single
durable run artifact.

### 2. Merge and PR Completion Escalated Approved Work

Several stories passed review and still failed in completion.

#### `issue-26`: branch protection mismatch

`issue-26` produced an approved result and created PR `#429`, but the final
merge failed because the repository policy prohibited the direct merge mode.
The log explicitly recommends `--auto` or `--admin`. This was not an
implementation correctness problem; it was a completion-mode mismatch between
`merge-pr` and branch protection.

Primary evidence:

- [`issue-26/audit.yaml`](../../.forge/logs/v0.5.0/issue-26/audit.yaml)
- [`run.log`](../../.forge/logs/v0.5.0/run.log)

#### `issue-227`: stale or contaminated branch history

`issue-227` was approved, but deferred merge failed during rebase onto `main`.
The handoff identifies a story commit for the fix, but the rebase failure
occurred on a different commit entirely, indicating the branch being rebased
contained unrelated history. That points to branch-state contamination rather
than a defect in the intended story code.

Primary evidence:

- [`issue-227/audit.yaml`](../../.forge/logs/v0.5.0/issue-227/audit.yaml)
- [`handoff-iter-1.yaml`](../../.forge/logs/v0.5.0/issue-227/handoff-iter-1.yaml)

#### `issue-360`: zero-delta branch still treated as mergeable work

`issue-360` is the clearest no-op completion failure. The handoff says no code
changes were required because the fix already existed, review agreed, and then
PR creation failed with `No commits between main and feat/issue-360`.

The problem is not that review was wrong. The problem is that the completion
path still attempted PR creation after concluding there was no story-specific
delta left to merge.

Primary evidence:

- [`issue-360/handoff-iter-1.yaml`](../../.forge/logs/v0.5.0/issue-360/handoff-iter-1.yaml)
- [`issue-360/audit.yaml`](../../.forge/logs/v0.5.0/issue-360/audit.yaml)

### 3. Dev Retries Claimed Success Without Corresponding Code Changes

The two most expensive escalations in the final run were `issue-322` and
`issue-366`. Both failed with:

- repeated reviewer P1 findings across cycles
- handoffs claiming the findings had been addressed
- final escalation message: `Dev retry produced no changes`

#### `issue-322`

The story was supposed to add ground-truth git metadata capture and handoff
cross-checking. The dev output and later handoff claimed the required helpers
and prompt sections had been added, but both review cycles found the same
missing functions and prompt updates. The final retry still produced no
material code change.

Primary evidence:

- [`issue-322/review-cycle-1/synthesized.yaml`](../../.forge/logs/v0.5.0/issue-322/review-cycle-1/synthesized.yaml)
- [`issue-322/review-cycle-2/synthesized.yaml`](../../.forge/logs/v0.5.0/issue-322/review-cycle-2/synthesized.yaml)
- [`issue-322/audit.yaml`](../../.forge/logs/v0.5.0/issue-322/audit.yaml)

#### `issue-366`

The story was supposed to add `{base_branch}` substitution and `--base-branch`
overrides. The handoff claimed those changes were implemented, but both review
cycles still found the same missing `create_command.format(..., base_branch=...)`
support and missing CLI flags. The final retry again produced no effective
change.

Primary evidence:

- [`issue-366/review-cycle-1/synthesized.yaml`](../../.forge/logs/v0.5.0/issue-366/review-cycle-1/synthesized.yaml)
- [`issue-366/review-cycle-2/synthesized.yaml`](../../.forge/logs/v0.5.0/issue-366/review-cycle-2/synthesized.yaml)
- [`issue-366/audit.yaml`](../../.forge/logs/v0.5.0/issue-366/audit.yaml)

#### Pattern

These stories show a shared control weakness: the dev loop can treat patch-like,
diff-like, or self-reported output as a successful retry before verifying that a
real change landed in the worktree and that the relevant findings were actually
addressed.

### 4. Hard Conventions Dominated Several Escalations

Two final-run failures were straightforward hard-convention violations.

#### `issue-326`

The story kept failing `max_module_lines` because
`src/theforge/runners/cli.py` remained at 521 lines against a 500-line limit.
The third dev log explicitly says the file was reduced but still not under the
limit.

Primary evidence:

- [`issue-326/audit.yaml`](../../.forge/logs/v0.5.0/issue-326/audit.yaml)
- [`dev-iter-3-dev.log`](../../.forge/logs/v0.5.0/issue-326/dev-iter-3-dev.log)

#### `issue-348`

The same thing happened for `src/theforge/cli/sprint.py`, which still measured
508 lines against the 500-line limit when the story escalated.

Primary evidence:

- [`issue-348/audit.yaml`](../../.forge/logs/v0.5.0/issue-348/audit.yaml)
- [`dev-iter-3-dev.log`](../../.forge/logs/v0.5.0/issue-348/dev-iter-3-dev.log)

#### Pattern

The current retry loop treats these as normal dev retries, but they behave more
like structural refactor tasks. When a story is only slightly over a hard line
limit, three full retries can be wasted without any dedicated strategy for
extracting code or splitting the file.

### 5. Preflight Was Too Permissive on Invalid Outputs

In the final run, malformed or unsupported preflight outputs still advanced into
full execution. The log shows cases where the preflight agent returned
`REQUEST_CHANGES` or failed, and the coordinator still converted that to
`PROCEED`.

That means ambiguous or invalid story classification did not stop work from
continuing into planning, dev, and review.

Primary evidence:

- [`run.log`](../../.forge/logs/v0.5.0/run.log)

### 6. Audit Fields Were Internally Inconsistent

The logs also exposed several reporting inconsistencies:

- convention failures are presented as `VALIDATE PASS` and then immediately as
  blocking convention violations in the run log, which is confusing for audit
  consumers
- dev invocation counts and `dev_iterations` do not cleanly reflect the amount
  of retry activity in the no-change stories
- stale artifacts from earlier reruns remain side-by-side with final-run
  artifacts in the same release log folder

These are not merely cosmetic issues. They weaken the audit trail exactly when
the system most needs a clean forensic record.

## Proposed Action Items

### Immediate

1. Write one run log per sprint launch instead of appending all reruns into a
   shared `run.log`.
2. Record a stable parent/index artifact for reruns so the final summary can
   point to each attempt explicitly.
3. Before PR creation, check whether `main..branch` is empty and convert that
   path to `DONE` or `ALREADY_DONE` instead of escalating.
4. Make `merge-pr` branch-protection aware by using auto-merge where policy
   requires it.
5. Revalidate branch state immediately before merge in parallel sprint runs.

### Near-Term

1. Tighten the dev retry guard so a retry is only accepted if the worktree
   actually changed and the changed files plausibly relate to the open findings.
2. Fail closed on malformed preflight output and on preflight execution failure
   instead of coercing those cases to `PROCEED`.
3. Add an explicit no-op completion path for stories that review as already
   implemented.
4. Improve branch hygiene checks so completion does not attempt rebases or PR
   creation on branches carrying unrelated history.
5. Make validation reporting atomic so `VALIDATE` cannot appear to both pass and
   fail in the same phase outcome.

### Structural

1. Add a dedicated remediation path for hard-convention failures such as
   `max_module_lines`, with extraction-oriented instructions rather than generic
   retry prompts.
2. Add audit assertions that compare run-level story counts, summary counts, and
   per-story artifact counts for consistency.
3. Make rerun cleanup or namespacing explicit so stale artifacts cannot be
   mistaken for part of the final release run.
4. Add tests for the no-op merge path, branch-protection-aware merge-pr path,
   and preflight fail-closed behavior.
