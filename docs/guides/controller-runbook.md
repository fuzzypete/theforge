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
valid until token *length* is checked. Remedy: re-authenticate the CLI
(`claude` → `/login`) — and be aware that doing so points the same conflict the
other way for whichever consumer signed in first.

If the credential is revoked *mid-sprint*, a circuit breaker trips on the first
fatal auth failure: in-flight workers are stopped and no further story is
dispatched, rather than re-presenting the same rejected credential once per
story and per phase. Stories the breaker cancels are recorded **SKIPPED** and
attributed to the credential, not FAILED — the sprint killed them, no model
judged them, and they contribute nothing to adaptive memory.

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

## 3. The `base_branch` trap (issue #1928) — read this

`workspace.base_branch` defaults to `main`. After every merged PR, forge's
"best-effort local cleanup" (`coordinator/completion.py`) runs, in the
**project-root checkout**:

```
git fetch origin
git merge --ff-only origin/<base_branch>
```

If you are checked out on a **release branch** while `base_branch` is still
`main`, that fast-forward silently drags your release branch up to
`origin/main` — importing main's dev version, its `[Unreleased]` changelog, and
main's forward-port merge commits. It corrupts the release branch **with no
error** (the step swallows failures as "non-fatal").

Until #1928 lands:

- Keep `--base-branch` matching the branch you have checked out, **or** don't
  sit on a release branch in the project root while a sprint runs.
- Symptom to watch for: `git show release/vX.Y:pyproject.toml` reads main's dev
  version instead of the branch's `rc` version.

## 4. Cutting a release candidate

```bash
RC_NUM=n scripts/cut-rc.sh X.Y.Z
```

- Reads the current version on the release branch, **overwrites** it to
  `X.Y.Zrcn`, runs the gate, commits, tags, pushes, and configures branch
  protection so auto-merge works. A stale version on the branch is therefore
  harmless — cut-rc overwrites it.
- `cut-rc.sh` only `pull --ff-only`s the *release* branch and creates it from
  `main` when it doesn't exist yet; it **never** merges `main` into an existing
  release branch. (So cut-rc is never the cause of a release branch picking up
  main's history — see §3 for what is.)
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
