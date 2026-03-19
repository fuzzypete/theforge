---
name: "Release automation — changelog generation and GitHub Releases"
slug: release-automation
pytest_target: tests/
---

# Release Automation

## Problem

Release notes are currently hand-written. As release cadence increases, this
becomes a bottleneck and a source of inconsistency. GitHub's Releases page is
the primary discovery surface for new users evaluating the project.

## Solution

1. Add `.github/release.yml` to configure GitHub's auto-generated release notes
   (categorize PRs by label into sections: Features, Fixes, Maintenance).

2. Add a `make release VERSION=0.2.0` target that:
   - Updates version in `pyproject.toml`
   - Appends a new section to `CHANGELOG.md` from git log since last tag
   - Creates a commit + annotated tag
   - Creates a GitHub Release with notes from CHANGELOG

3. Add a `.github/workflows/release.yml` workflow that triggers on tag push
   (`v*`) and creates the GitHub Release automatically with auto-generated
   notes from merged PRs.

## Acceptance criteria

- [ ] `.github/release.yml` categorizes PRs into Features, Fixes, Maintenance, Docs
- [ ] `make release VERSION=X.Y.Z` updates pyproject.toml version, CHANGELOG.md, commits, and tags
- [ ] `.github/workflows/release.yml` creates GitHub Release on tag push
- [ ] Release notes include PR titles grouped by category
- [ ] Existing tests pass
- [ ] Manual test: `make release VERSION=0.1.1` produces correct tag and changelog entry
