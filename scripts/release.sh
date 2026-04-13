#!/usr/bin/env bash
# scripts/release.sh — cut a TheForge release
#
# Usage: scripts/release.sh [--dry-run] [VERSION]
#   VERSION  The version to release, e.g. 0.5.0 (default: pyproject.toml version minus .dev0)
#
# Follows the process documented in RELEASING.md.

set -euo pipefail

# Ensure Homebrew bin is on PATH (Apple Silicon default location)
export PATH="/opt/homebrew/bin:$PATH"

DRY_RUN=false
VERSION=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        *) VERSION="$arg" ;;
    esac
done

# Read current version from pyproject.toml
CURRENT_VERSION=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')

if [[ -z "$VERSION" ]]; then
    VERSION="${CURRENT_VERSION%.dev0}"
fi

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: VERSION must be X.Y.Z (got: $VERSION)" >&2
    exit 1
fi
NEXT_DEV="$(echo "$VERSION" | awk -F. '{print $1"."$2+1".0.dev0"}')"

echo "Current version : $CURRENT_VERSION"
echo "Releasing       : $VERSION"
echo "Next dev        : $NEXT_DEV"
echo "Dry run         : $DRY_RUN"
echo ""

run() {
    echo "+ $*"
    if [[ "$DRY_RUN" == false ]]; then
        "$@"
    fi
}

# --- 1. Verify milestone is complete ---
echo "==> Checking milestone v$VERSION..."
OPEN_ISSUES=$(gh issue list --repo fuzzypete/theforge --milestone "v$VERSION" --state open --json number --jq 'length')
if [[ "$OPEN_ISSUES" != "0" ]]; then
    echo "Error: $OPEN_ISSUES open issue(s) remain in milestone v$VERSION. Close them before releasing." >&2
    gh issue list --repo fuzzypete/theforge --milestone "v$VERSION" --state open >&2
    exit 1
fi
echo "    Milestone clean."

# --- 2. Verify clean main ---
echo "==> Verifying clean state..."
run git checkout main
run git pull --ff-only

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Error: working tree is dirty. Commit or stash changes before releasing." >&2
    exit 1
fi

# --- 3. Gate ---
echo "==> Running gate..."
run make gate

# --- 4. Update CHANGELOG ---
echo "==> Updating CHANGELOG..."
TODAY=$(date +%Y-%m-%d)
if [[ "$DRY_RUN" == false ]]; then
    # Rename [Unreleased] → [VERSION] — DATE and add new [Unreleased] above
    sed -i '' \
        "s/^## \[Unreleased\]/## [$VERSION] — $TODAY/" \
        CHANGELOG.md
    # Insert new [Unreleased] section above the versioned one
    sed -i '' \
        "/^## \[$VERSION\]/i\\
## [Unreleased]\\
\\
" \
        CHANGELOG.md
fi
echo "    CHANGELOG updated."

# --- 5. Bump version in pyproject.toml ---
echo "==> Bumping version to $VERSION..."
if [[ "$DRY_RUN" == false ]]; then
    sed -i '' "s/^version = \"$CURRENT_VERSION\"/version = \"$VERSION\"/" pyproject.toml
fi
echo "    pyproject.toml updated."

# --- 6. Commit ---
echo "==> Committing..."
run git add CHANGELOG.md pyproject.toml
run git commit -m "chore: release v$VERSION"

# --- 7. Tag and push ---
echo "==> Tagging and pushing..."
run git tag "v$VERSION"
run git push origin main
run git push origin "v$VERSION"

# --- 8. Cut release branch ---
echo "==> Creating release branch release/v$(echo "$VERSION" | cut -d. -f1,2)..."
RELEASE_BRANCH="release/v$(echo "$VERSION" | cut -d. -f1,2)"
run git checkout -b "$RELEASE_BRANCH"
run git push origin "$RELEASE_BRANCH"
run git checkout main

# --- 9. Bump main to dev ---
echo "==> Bumping main to $NEXT_DEV..."
if [[ "$DRY_RUN" == false ]]; then
    sed -i '' "s/^version = \"$VERSION\"/version = \"$NEXT_DEV\"/" pyproject.toml
fi
run git add pyproject.toml
run git commit -m "chore: begin v$NEXT_DEV development [skip ci]"
run git push origin main

# --- 10. GitHub Release ---
echo "==> Creating GitHub release..."
RELEASE_NOTES=$(awk "/^## \[$VERSION\]/{found=1; next} found && /^## \[/{exit} found{print}" CHANGELOG.md)
run gh release create "v$VERSION" --repo fuzzypete/theforge \
    --title "v$VERSION" \
    --notes "$RELEASE_NOTES"

# --- 11. Create post-release doc review issue ---
echo "==> Creating post-release doc review issue for $NEXT_MILESTONE..."
NEXT_MILESTONE="v$(echo "$VERSION" | awk -F. '{print $1"."$2+1".0"}')"
DOC_REVIEW_BODY="## What

Review all public-facing documentation after v$VERSION ships to ensure it matches what was released.

## Why

v$VERSION is a new release. Documentation written before or during development may describe stale behavior, missing flags, or outdated architecture. This review ensures users see accurate docs.

## Checklist

- [ ] **README.md** — capabilities, install instructions, CLI usage
- [ ] **CHANGELOG.md** — release section accurately covers what shipped
- [ ] **CLAUDE.md** — conventions, architecture notes, phase descriptions
- [ ] **AGENTS.md** — agent instructions match current prompt construction and tool usage
- [ ] **RELEASING.md** — process gaps from this release
- [ ] **forge.yaml comments** — inline config docs match current behavior
- [ ] **CLI help text** — \`forge --help\` and subcommands reflect current flags
- [ ] **GitHub release notes** — body covers what users need to know"
run gh issue create --repo fuzzypete/theforge \
    --title "Post-release doc review for v$VERSION" \
    --body "$DOC_REVIEW_BODY" \
    --label "documentation" \
    --milestone "$NEXT_MILESTONE"

echo ""
echo "Released v$VERSION."
