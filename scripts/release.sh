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
UNTRIAGED_FINDINGS=$(gh issue list --repo fuzzypete/theforge --milestone "v$VERSION" --state open --label "forge-finding" --label "needs-triage" --json number --jq 'length')
echo "    Open needs-triage forge-findings: $UNTRIAGED_FINDINGS"
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
NEXT_MILESTONE="v$(echo "$VERSION" | awk -F. '{print $1"."$2+1".0"}')"
echo "==> Creating post-release doc review issue for $NEXT_MILESTONE..."
DOC_REVIEW_BODY="## What

Review every public-facing and decision-preserving doc after v$VERSION ships. Verify each concrete claim against the current code, CLI, or schema — not against another doc. Record every mismatch as a drift entry in this issue before closing.

## Why

Docs drift past release reviews when reviewers check whether docs agree with each other or simply \"read neatly,\" instead of checking whether the claims match the system. Recent concrete example: \`docs/guides/inputs-reference.md\` documented a deprecated \`pytest_target\` frontmatter field through multiple releases — it survived every review because reviewers triangulated across CLAUDE.md, AGENTS.md, and other guides instead of comparing against the current schema. This template forces verification against code and makes drift findings the required output.

## How to verify (read before ticking anything)

For every doc in the checklist below, verify claims **against the system**, not against other docs:

- For every field name in a doc → grep the source, compare to the current dataclass / schema / frontmatter parser
- For every CLI command, flag, or subcommand → run it, paste the actual output into your drift notes, compare
- For every file or directory path referenced → confirm it exists at that path in the current working tree
- For every example config, story, issue, or YAML snippet → run it through the relevant validator (shape-check, \`forge validate\`, config loader) and confirm it passes
- For every architectural or behavioral claim → locate the code that implements it and confirm the doc's description still matches

\"It agrees with CLAUDE.md / AGENTS.md / another guide\" is **not** verification — those are docs too and may themselves be stale. The only passing signal is \"I ran it / read the code and it matches.\"

## Scope — docs to review

- [ ] **README.md** — capabilities, install instructions, quick start
- [ ] **CHANGELOG.md** — v$VERSION section accurately covers what shipped
- [ ] **CLAUDE.md** (root and all directory-level) — conventions, phase descriptions, invariants
- [ ] **AGENTS.md** — agent instructions match current prompt construction and tool usage
- [ ] **RELEASING.md** — process gaps from this release
- [ ] **forge.yaml** — inline comments match current schema and behavior
- [ ] **CLI help text** — \`forge --help\` and every subcommand reflect current flags
- [ ] **GitHub release notes** — body covers what users need to know
- [ ] **docs/guides/** — every file (authoring, CLI reference, inputs reference, getting started, provider setup, model reference, troubleshooting, local models, first-run walkthrough)
- [ ] **docs/vision.md** and anything under **docs/vision/** — reflect current direction, not abandoned paths
- [ ] **docs/plans/** — active plans are still active; completed or abandoned plans are archived or marked
- [ ] **docs/postmortems/** — referenced issue numbers and dates resolve; follow-ups are closed or tracked

## Required output — drift report

Before closing this issue, add a comment containing a drift report with one entry per mismatch found:

\`\`\`
<path/to/doc>:<line> — doc says \"<X>\" — code/CLI/schema says \"<Y>\"
\`\`\`

For each drift entry, either:
- Patch the doc in a follow-up PR and link it, **or**
- File a follow-up issue and link it here

An empty drift report is acceptable — it means every claim was checked and confirmed. But \"empty\" must be an explicit comment (\"no drift found — verified against X, Y, Z\"), not a missing one. A missing drift report blocks closure.

## Acceptance criteria

- A drift-report comment exists on this issue listing every mismatch found, in the format above, or explicitly stating \"no drift found\" with a list of the verification steps that were actually executed
- For each drifted line, either a patch PR or a follow-up issue is linked in the drift report
- The verification method for each reviewed doc is auditable from the drift report: which command was run, which file/schema was read, not just \"I reviewed it\"
- No checklist item is marked done on the basis of agreement with another doc
- Every checkbox in the scope list above is ticked before the issue closes"
run gh issue create --repo fuzzypete/theforge \
    --title "Post-release doc review for v$VERSION" \
    --body "$DOC_REVIEW_BODY" \
    --label "documentation" \
    --milestone "$NEXT_MILESTONE"

echo ""
echo "Released v$VERSION."
