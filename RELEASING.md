# Release Process

This document defines how TheForge releases are cut, maintained, and hotfixed.

## Operating model

- **Milestone = release scope.** `v0.4.0` milestone on GitHub contains exactly the
  issues that ship in that version. "What's in this release?" is answered by
  `gh issue list --milestone v0.4.0 --state closed`.
- **Package version = shipped artifact.** `pyproject.toml` holds the last released
  version (or `X.Y.Z.dev0` between releases). It is never bumped ahead of the tag.
- **`[Unreleased]` accumulates.** CHANGELOG has an `[Unreleased]` section that grows
  as work lands on `main`. At release time it is renamed to the version + date.
- **Sprints run against the right base branch.** `forge.yaml` (or an override config)
  sets `workspace.base_branch`. For main-line development this is `main`; for
  release-branch work it is `release/vX.Y`.

---

## Cutting a new release

Use the release script — it handles all steps automatically:

```bash
scripts/release.sh X.Y.Z
```

Use `--dry-run` to preview what it will do without making changes:

```bash
scripts/release.sh --dry-run X.Y.Z
```

The script: verifies the milestone is closed, runs `make gate`, updates
CHANGELOG, bumps `pyproject.toml`, tags and pushes, cuts the release branch,
bumps main to the next dev version, and creates the GitHub release.

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

### 3. Update CHANGELOG

Rename `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`.

Add a new `## [Unreleased]` section at the top for future work.

Before continuing, verify the release section against both the milestone and the
tag range:

```bash
gh issue list --milestone vX.Y.Z --state closed --limit 200
git log --oneline <previous-release-tag>..HEAD
```

The GitHub release body is generated from this CHANGELOG section, so missing
items here become missing release notes.

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
