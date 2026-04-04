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
  --notes-from-tag \
  --notes "$(awk '/^## \[X\.Y\.Z\]/,/^## \[/' CHANGELOG.md | head -n -1)"
```

Or create manually via GitHub UI using the CHANGELOG section as release notes.

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

If the fix is non-trivial, use forge against the release branch. Create a
`forge-release.yaml` (or copy `forge.yaml` and set `workspace.base_branch: release/vX.Y`):

```yaml
workspace:
  base_branch: release/vX.Y
  create_command: "git worktree add .forge/worktrees/{slug} -b fix/{slug} release/vX.Y"
  # ... rest of config
```

Then: `forge sprint --config forge-release.yaml --milestone vX.Y.Z+1 --budget 20`

> **Note:** `{base_branch}` substitution in `create_command` is a planned v0.5.0
> improvement. Until then, set the branch explicitly in `create_command`.

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
