# Release Process

This document defines how TheForge releases are cut, maintained, and hotfixed.

## Operating model

- **Milestone = release scope.** `v0.4.0` milestone on GitHub contains exactly the
  issues that ship in that version. "What's in this release?" is answered by
  `gh issue list --milestone v0.4.0 --state closed`.
- **Package version = shipped artifact.** `pyproject.toml` holds the last released
  version (or `X.Y.Z.dev0` between releases). It is never bumped ahead of the tag.
- **The release CHANGELOG section is derived, not hand-curated.** At promote time
  `promote-rc.sh` derives the `[X.Y.Z]` section from the milestone's closed issues
  cross-referenced against the PR merges in the previous-tag..HEAD range, and
  aborts the promote if the two disagree. The `[Unreleased]` section is now an
  optional working-notes scratchpad — operators may scribble context there during
  development, but its contents are not consulted for release notes and it is left
  untouched at promote time.
- **Sprints run against the right base branch.** `forge.yaml` (or an override config)
  sets `workspace.base_branch`. For main-line development this is `main`; for
  release-branch work it is `release/vX.Y`.

---

## Cutting a new release — RC flow (preferred)

Releases ship via a release-candidate ladder so dogfood sprints exercise the
candidate against the next milestone's stories before the final tag is cut.
The pattern: cut an RC into an isolated dogfood substrate, point plain
`forge` at it, run a small sprint ladder, promote when the ladder passes
(or cut another RC if it doesn't).

### Operator one-time setup: managed `forge` launcher

`cut-rc.sh` repoints plain `forge` at the just-cut RC by writing a symlink
to a managed launcher path (default: `~/.local/bin/forge`, overridable via
`FORGE_MANAGED_LAUNCHER`). For this to work, the launcher's directory must
be on `PATH` **ahead of any Python environment's `bin/`**. If a venv is
activated whose `bin/forge` precedes the managed launcher, `cut-rc.sh`
refuses to clobber it (a `pip install -e .` would regenerate the file and
silently undo the cut-RC binding) and prints guidance.

The simplest setup: ensure your shell's PATH puts `~/.local/bin` before any
project venv `bin/`, or deactivate the venv before running `cut-rc.sh`.

`cut-rc.sh` also seeds the isolated RC env from the project's pinned
interpreter: by default `./.venv/bin/python` (falling back to
`./.venv/bin/python3` if present). If the project venv is missing or you need a
different pinned interpreter, set `FORGE_RC_PYTHON=/abs/path/to/python` before
running the cut. The script refuses to fall back to an unqualified `python3`
lookup, because that can silently bind the RC runtime to an unrelated system
Python that happened to appear on `PATH`.

### 1. Cut an RC

```bash
scripts/cut-rc.sh X.Y.Z              # first RC: cuts vX.Y.ZrcN with N=0
scripts/cut-rc.sh X.Y.Z 1            # subsequent RCs: explicit RC_NUM
scripts/cut-rc.sh --dry-run X.Y.Z
scripts/cut-rc.sh --no-install X.Y.Z # skip the isolated-venv install + launcher repoint
scripts/cut-rc.sh --resume X.Y.Z N   # finish a cut that aborted after tag-push
```

`--resume` is the recovery path when a cut aborts at one of the post-tag steps
(isolated venv install, managed-launcher repoint, ladder print). Because the
tag has already been pushed to origin at that point, re-running the plain
invocation fails on the `git tag` step. `--resume` skips bump/commit/tag/push,
asserts the RC tag is already on origin, and picks up at the isolated-venv
install. The launcher-refusal error messages the script prints today point at
this invocation — fix the env-state issue they describe, then `--resume`.

`cut-rc.sh`:

- prints the open issues remaining in milestone `vX.Y.Z` (informational only —
  RC cuts deliberately do **not** require a clean milestone)
- creates or fast-forwards `release/vX.Y` from `main`
- bumps `pyproject.toml` to `X.Y.ZrcN` (PEP 440)
- runs `make gate`
- commits, tags `vX.Y.ZrcN`, pushes branch and tag
- creates an isolated venv at `.forge/rc-envs/vX.Y.ZrcN/` and installs the
  RC into it (no other Python environment is touched)
- repoints the managed `forge` launcher at the new RC venv, so plain
  `forge --version` reports the just-cut RC
- prints the test ladder using plain `forge`

The launcher repoint is intentional: cutting an RC and dogfooding it are
the same muscle-memory action, and the symlink keeps the substrate
isolated from source-tree edits. Use `--no-install` if you need to install
on a different machine or env (the managed launcher is left untouched).

### 2. Run the test ladder against the RC

The ladder is three sprints of ascending complexity, all run on TheForge's own
repo against the RC binary:

| Pass       | Story shape                                              |
|------------|----------------------------------------------------------|
| Smoke      | Small, well-scoped story (low spend, gross-breakage check) |
| Boundary   | Medium-complexity story touching CLI/precondition surface |
| Moneyshot  | High-complexity story (exercises adaptive routing decisions) |

```bash
forge sprint --verbose --issues <small-issue>            --budget 50 --parallel 1
forge sprint --verbose --issues <medium-issue>           --budget 50 --parallel 1
forge sprint --verbose --issues <high-complexity-issue>  --budget 50 --parallel 1
```

Pick the candidates from the **next** milestone (so the work also makes progress
on the next release) — exact picks are per-release, not scripted.

Watch for: regressions in any behavior shipped by `vX.Y.Z`, surprising routing
decisions, missing or wrong audit/log wording, anything that contradicts the
release's CHANGELOG entry.

### 3. Either fix or promote

- **Ladder passes** → `scripts/promote-rc.sh X.Y.Z`
- **Ladder fails** → fix on `release/vX.Y`, then `scripts/cut-rc.sh X.Y.Z N+1`

### 4. Promote the RC to a final release

```bash
scripts/promote-rc.sh X.Y.Z
scripts/promote-rc.sh --dry-run X.Y.Z
```

`promote-rc.sh`:

- requires you are on `release/vX.Y` with `pyproject.toml` version = `X.Y.ZrcN`
- requires milestone `vX.Y.Z` has zero open issues (this is where the milestone
  block lives, not in `cut-rc.sh`)
- runs `make gate`
- derives the `[X.Y.Z] — YYYY-MM-DD` CHANGELOG section from milestone `vX.Y.Z` +
  the PR merges in `<previous-tag>..HEAD`, aborting on any milestone/merge
  mismatch, and splices it above the existing sections (leaving `[Unreleased]`
  and its notes untouched). `promote-rc.sh --dry-run X.Y.Z` previews the derived
  section without writing.
- bumps `pyproject.toml` from `X.Y.ZrcN` to `X.Y.Z`
- commits, tags `vX.Y.Z`, pushes
- bumps `main` to the next `.dev0` version
- creates the GitHub release from the CHANGELOG section
- files the post-release doc-review issue against the next milestone

After promotion, the script prints reminders for the manual follow-ups it
intentionally does **not** automate:

1. Forward-port any RC fixes from `release/vX.Y` to `main` if main has diverged.
2. Run the post-release doc review.

(The managed `forge` launcher continues to point at the last-cut RC venv,
which contains the same code as the just-promoted final tag. If you want
plain `forge` to track the final tag specifically, cut a fresh venv from
`vX.Y.Z` and `ln -snf` the managed launcher at it.)

### Direct release from main (legacy)

`scripts/release.sh X.Y.Z` still exists for releasing directly from main without
an RC ladder. Use only when an RC dogfood pass would add no signal — for
example, a one-line patch. Default path is the RC flow above.

### Manual steps (reference)

If you need to run steps individually:

### 1. Verify milestone is complete

```bash
gh issue list --milestone vX.Y.Z --state open
# Should return nothing. All milestone issues must be closed.
```

### 2. Pull and verify clean state

```bash
git checkout main && git pull --ff-only
make gate   # must pass
```

### 3. CHANGELOG (derived automatically)

You do **not** hand-write the release section. `promote-rc.sh` derives the
`## [X.Y.Z] — YYYY-MM-DD` section by calling `scripts/derive_changelog.py`,
which lists the milestone's closed issues, cross-references them against the PR
merges in `<previous-tag>..HEAD`, and:

- aborts the promote if a milestone issue has no corresponding merge, or a merged
  PR (`(#N)` in its squash subject) has no milestone issue — surfacing the exact
  discrepancy so you can fix milestone/PR linkage and retry;
- otherwise groups entries by issue label (`bug`→Fixed, `enhancement`→Added,
  `documentation`→Documentation, everything else→Changed) as `- Title (#N)`.

Preview the derived section without writing anything:

```bash
scripts/promote-rc.sh --dry-run X.Y.Z
```

The GitHub release body is generated from this derived section, so the milestone
and the tag range are the source of truth — a missing item is a milestone/PR
linkage gap, not a CHANGELOG-editing miss. `[Unreleased]` is left untouched as an
optional scratchpad and is not consulted.

### 4. Bump version

In `pyproject.toml`, change `version = "X.Y.Z.dev0"` to `version = "X.Y.Z"`.

### 5. Commit

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "chore: release vX.Y.Z"
```

### 6. Tag and push

```bash
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

### 7. Cut the release branch

```bash
git checkout -b release/vX.Y
git push origin release/vX.Y
```

This branch is the maintenance line for X.Y.x hotfixes.

### 8. Bump main to dev

Back on main, bump `pyproject.toml` to the next dev version and add a new
`## [Unreleased]` section to CHANGELOG if not already present:

```bash
git checkout main
# edit pyproject.toml: version = "X.Y+1.0.dev0" (or X.Y.1.dev0 if patch)
git add pyproject.toml
git commit -m "chore: begin vX.Y+1.0.dev0 development"
git push origin main
```

### 9. Create GitHub Release

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes "$(awk "/^## \[$VERSION\]/{found=1; next} found && /^## \[/{exit} found{print}" CHANGELOG.md)"
```

Or create manually via GitHub UI using the CHANGELOG section as release notes.

After creation, verify the published body before starting the next sprint:

```bash
gh release view vX.Y.Z
```

If the body is incomplete, fix `CHANGELOG.md` first, then update the release:

```bash
gh release edit vX.Y.Z --notes-file <(awk "/^## \[$VERSION\]/{found=1; next} found && /^## \[/{exit} found{print}" CHANGELOG.md)
```

---

## Post-release doc review

After cutting a release, review public-facing documentation before the next sprint
starts. The release tag is what users see — docs should match what shipped.

### Checklist

- [ ] **README.md** — capabilities, install instructions, CLI usage match current state
- [ ] **CHANGELOG.md** — release section accurately covers what shipped
- [ ] **CONVENTIONS.md** (root and all directory-level) — conventions, architecture notes, phase descriptions, invariants match shipped code
- [ ] **CLAUDE.md / AGENTS.md** — agent-specific pointer docs and harness notes match shipped code
- [ ] **RELEASING.md** — did this release surface any process gaps? Update if so
- [ ] **forge.yaml comments** — inline config docs match current behavior
- [ ] **CLI help text** — `forge --help` and subcommand help reflect current flags and options
- [ ] **GitHub release notes** — body covers what users need to know

If anything is materially wrong, file a story for the next milestone. For small
documentation fixes, create a small issue/worktree pair and still keep the audit
trail intact.

---

## Hotfixing a release (vX.Y.Z → vX.Y.Z+1)

Use this when a bug is found in a shipped release and cannot wait for the next
minor release.

### 1. Branch from the release line

```bash
git checkout release/vX.Y
git pull origin release/vX.Y
git checkout -b fix/my-hotfix
```

### 2. Fix and test

Make the fix, run `make gate`. Commit normally.

### 3. Forge the fix (optional but preferred)

If the fix is non-trivial, use forge against the release branch. Ensure your
`forge.yaml` uses `{base_branch}` in `workspace.create_command`, then override the
base branch at invocation time:

```yaml
workspace:
  base_branch: main
  create_command: "git worktree add .forge/worktrees/{slug} -b fix/{slug} {base_branch}"
  # ... rest of config
```

Then: `forge sprint --base-branch release/vX.Y --milestone vX.Y.Z+1 --budget 20`

### 4. PR and merge to release branch

PR targets `release/vX.Y`. Merge when approved.

### 5. Bump version and tag

On `release/vX.Y`:

```bash
# edit pyproject.toml: version = "X.Y.Z+1"
# update CHANGELOG: rename [Unreleased] to [X.Y.Z+1] — YYYY-MM-DD
git add CHANGELOG.md pyproject.toml
git commit -m "chore: release vX.Y.Z+1"
git tag vX.Y.Z+1
git push origin release/vX.Y
git push origin vX.Y.Z+1
```

### 6. Cherry-pick to main

```bash
git checkout main
git cherry-pick <fix-commit-sha>
git push origin main
```

If the fix does not apply cleanly (main has diverged significantly), port it
manually and open a PR against main.

### 7. Create GitHub Release

```bash
gh release create vX.Y.Z+1 --title "vX.Y.Z+1 — hotfix" --notes "..."
```

---

## Release branch naming

| Branch | Purpose |
|--------|---------|
| `main` | Active development, always releasable |
| `release/vX.Y` | Maintenance line for vX.Y.x releases |
| `feat/<slug>` | Feature branches created by forge (merge to main) |
| `fix/<slug>` | Hotfix branches (merge to release/vX.Y) |

---

## FAQ

**Can I run a v0.5.0 sprint while v0.4.0 is being released?**
Yes. Tag v0.4.0, then sprint v0.5.0 stories against main. The tag is immutable.

**What if forge finds bugs in a story that just landed on main?**
If the bug is pre-release (milestone still open): forge a fix story against main,
slot it into the milestone, and re-verify before tagging.
If the bug is post-release: hotfix via the release branch process above.

**Does `on_approve: merge-pr` work for release-branch hotfixes?**
Yes, if `forge.yaml` (or override config) sets `workspace.base_branch` to the
release branch. PRs will target that branch automatically.
