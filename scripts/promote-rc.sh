#!/usr/bin/env bash
# scripts/promote-rc.sh — promote a TheForge release candidate to a final release
#
# Usage: scripts/promote-rc.sh [--dry-run] VERSION
#   VERSION  The final version to release, e.g. 0.10.0
#
# Promotes the current release candidate (X.Y.ZrcN, on release/vX.Y) to
# the final X.Y.Z release: bumps pyproject, updates CHANGELOG, runs gate,
# tags vX.Y.Z, pushes, creates the GitHub release, bumps main to next dev,
# and files the post-release doc-review issue.
#
# Requires:
#   - currently on release/vX.Y branch
#   - pyproject.toml version is X.Y.ZrcN (an RC was previously cut)
#   - milestone vX.Y.Z has zero open issues
#
# Reminds the operator at the end to forward-port any RC-only fixes to main
# and to run the post-release doc review. The managed dogfood `forge`
# launcher (managed by cut-rc.sh) is intentionally not re-linked here — the
# last-RC venv code already matches the promoted final tag.
#
# See RELEASING.md for the end-to-end RC flow.

set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

DRY_RUN=false
VERSION=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        *)
            if [[ -z "$VERSION" ]]; then
                VERSION="$arg"
            else
                echo "Error: unexpected argument: $arg" >&2
                exit 2
            fi
            ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    echo "Error: VERSION required (e.g. 0.10.0)" >&2
    echo "Usage: scripts/promote-rc.sh [--dry-run] VERSION" >&2
    exit 2
fi

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: VERSION must be X.Y.Z (got: $VERSION)" >&2
    exit 2
fi

RELEASE_BRANCH="release/v$(echo "$VERSION" | cut -d. -f1,2)"
NEXT_DEV="$(echo "$VERSION" | awk -F. '{print $1"."$2+1".0.dev0"}')"
NEXT_MILESTONE="v$(echo "$VERSION" | awk -F. '{print $1"."$2+1".0"}')"

# CURRENT_VERSION is read after the release branch is pulled (step 3) so a
# stale local checkout can't pass the RC precondition with one version while
# the bump-and-tag operates on a different version.
CURRENT_VERSION=""
RC_NUM=""
RC_TAG=""

run() {
    echo "+ $*"
    if [[ "$DRY_RUN" == false ]]; then
        "$@"
    fi
}

echo "Promoting to    : $VERSION"
echo "Release branch  : $RELEASE_BRANCH"
echo "Next dev        : $NEXT_DEV"
echo "Next milestone  : $NEXT_MILESTONE"
echo "Dry run         : $DRY_RUN"
echo ""

# --- 1. Verify on release branch ---
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$RELEASE_BRANCH" ]]; then
    echo "Error: must be on $RELEASE_BRANCH (currently on $CURRENT_BRANCH)." >&2
    echo "       Run: git checkout $RELEASE_BRANCH" >&2
    exit 1
fi

# --- 2. Verify clean tree ---
echo "==> Verifying clean state..."
if [[ -n "$(git status --porcelain)" ]]; then
    echo "Error: working tree is dirty. Commit or stash changes before promoting." >&2
    exit 1
fi

# --- 3. Pull release branch BEFORE reading version ---
if git show-ref --verify --quiet "refs/remotes/origin/$RELEASE_BRANCH"; then
    run git pull --ff-only origin "$RELEASE_BRANCH"
fi

# --- 4. Read current version from the up-to-date release branch and validate it's an RC ---
CURRENT_VERSION=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
if [[ ! "$CURRENT_VERSION" =~ ^${VERSION}rc[0-9]+$ ]]; then
    echo "Error: current pyproject version on $RELEASE_BRANCH is $CURRENT_VERSION; expected ${VERSION}rcN." >&2
    echo "       Cut an RC first: scripts/cut-rc.sh $VERSION" >&2
    exit 1
fi
RC_NUM=$(echo "$CURRENT_VERSION" | sed "s/^${VERSION}rc//")
RC_TAG="v$CURRENT_VERSION"
echo "==> Current RC: $CURRENT_VERSION ($RC_TAG)"

# --- 5. Verify milestone is empty ---
echo "==> Checking milestone v$VERSION..."
OPEN_ISSUES=$(gh issue list --repo fuzzypete/theforge --milestone "v$VERSION" --state open --json number --jq 'length')
if [[ "$OPEN_ISSUES" != "0" ]]; then
    echo "Error: $OPEN_ISSUES open issue(s) remain in milestone v$VERSION. Close them before promoting." >&2
    gh issue list --repo fuzzypete/theforge --milestone "v$VERSION" --state open >&2
    exit 1
fi
echo "    Milestone clean."

# --- 6. Refuse if final tag already exists ---
if git rev-parse --verify --quiet "v$VERSION" >/dev/null; then
    echo "Error: tag v$VERSION already exists locally." >&2
    exit 1
fi
if git ls-remote --tags origin "refs/tags/v$VERSION" | grep -q "v$VERSION"; then
    echo "Error: tag v$VERSION already exists on origin." >&2
    exit 1
fi

# --- 7. Gate ---
echo "==> Running gate..."
run make gate

# --- 8. Update CHANGELOG ---
echo "==> Updating CHANGELOG..."
TODAY=$(date +%Y-%m-%d)
if [[ "$DRY_RUN" == false ]]; then
    sed -i '' \
        "s/^## \[Unreleased\]/## [$VERSION] — $TODAY/" \
        CHANGELOG.md
    sed -i '' \
        "/^## \[$VERSION\]/i\\
## [Unreleased]\\
\\
" \
        CHANGELOG.md
fi

# --- 9. Bump pyproject from RC to final ---
echo "==> Bumping pyproject.toml: $CURRENT_VERSION -> $VERSION..."
if [[ "$DRY_RUN" == false ]]; then
    sed -i '' "s/^version = \"$CURRENT_VERSION\"/version = \"$VERSION\"/" pyproject.toml
    BUMPED=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
    if [[ "$BUMPED" != "$VERSION" ]]; then
        echo "Error: pyproject bump failed — version is $BUMPED, expected $VERSION." >&2
        exit 1
    fi
fi

# --- 10. Capture release notes from the RELEASE BRANCH's CHANGELOG ---
# Must happen before checking out main, since main may only have [Unreleased].
echo "==> Capturing release notes from $RELEASE_BRANCH CHANGELOG..."
if [[ "$DRY_RUN" == false ]]; then
    RELEASE_NOTES=$(awk "/^## \[$VERSION\]/{found=1; next} found && /^## \[/{exit} found{print}" CHANGELOG.md)
    if [[ -z "$RELEASE_NOTES" ]]; then
        echo "Error: release notes for [$VERSION] are empty. Did the CHANGELOG bump produce a section?" >&2
        exit 1
    fi
else
    RELEASE_NOTES="(dry-run: would be extracted from CHANGELOG.md after bump)"
fi

# --- 11. Commit, tag, push release branch ---
echo "==> Committing, tagging, pushing..."
run git add CHANGELOG.md pyproject.toml
run git commit -m "chore: release v$VERSION"
run git tag "v$VERSION"
run git push origin "$RELEASE_BRANCH"
run git push origin "v$VERSION"

# --- 12. Bump main to next dev ---
echo "==> Bumping main to $NEXT_DEV..."
run git checkout main
run git pull --ff-only
MAIN_VERSION=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
if [[ "$DRY_RUN" == false ]]; then
    sed -i '' "s/^version = \"$MAIN_VERSION\"/version = \"$NEXT_DEV\"/" pyproject.toml
fi
run git add pyproject.toml
run git commit -m "chore: begin v$NEXT_DEV development [skip ci]"
run git push origin main

# --- 13. GitHub Release (using notes captured before main checkout) ---
echo "==> Creating GitHub release..."
run gh release create "v$VERSION" --repo fuzzypete/theforge \
    --title "v$VERSION" \
    --notes "$RELEASE_NOTES"

# --- 14. Post-release doc review issue ---
echo "==> Creating post-release doc review issue for $NEXT_MILESTONE..."
DOC_REVIEW_BODY="## What

Review every public-facing and decision-preserving doc after v$VERSION ships. Verify each concrete claim against the current code, CLI, or schema — not against another doc. Record every mismatch as a drift entry in this issue before closing.

## Why

Docs drift past release reviews when reviewers check whether docs agree with each other or simply \"read neatly,\" instead of checking whether the claims match the system. Recent concrete example: \`docs/guides/inputs-reference.md\` documented a deprecated \`pytest_target\` frontmatter field through multiple releases — it survived every review because reviewers triangulated across CONVENTIONS.md, CLAUDE.md, AGENTS.md, and other guides instead of comparing against the current schema. This template forces verification against code and makes drift findings the required output.

## How to verify (read before ticking anything)

For every doc in the checklist below, verify claims **against the system**, not against other docs:

- For every field name in a doc → grep the source, compare to the current dataclass / schema / frontmatter parser
- For every CLI command, flag, or subcommand → run it, paste the actual output into your drift notes, compare
- For every file or directory path referenced → confirm it exists at that path in the current working tree
- For every example config, story, issue, or YAML snippet → run it through the relevant loader or shape-check path exercised by the tests, and confirm it passes
- For every architectural or behavioral claim → locate the code that implements it and confirm the doc's description still matches

\"It agrees with CONVENTIONS.md / CLAUDE.md / AGENTS.md / another guide\" is **not** verification — those are docs too and may themselves be stale. The only passing signal is \"I ran it / read the code and it matches.\"

## Scope — docs to review

- [ ] **README.md** — capabilities, install instructions, quick start
- [ ] **CHANGELOG.md** — v$VERSION section accurately covers what shipped
- [ ] **CONVENTIONS.md** (root and all directory-level) — conventions, architecture notes, phase descriptions, invariants
- [ ] **CLAUDE.md / AGENTS.md** — agent-specific pointer docs and harness notes match current prompt construction and tool usage
- [ ] **RELEASING.md** — process gaps from this release
- [ ] **forge.yaml** — inline comments match current schema and behavior
- [ ] **CLI help text** — \`forge --help\` and every subcommand reflect current flags
- [ ] **GitHub release notes** — body covers what users need to know
- [ ] **docs/guides/** — every file currently present (run \`ls docs/guides/\`); do not work from a hardcoded list in this template, since files are added and renamed between releases
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

# --- 15. Reminders the script intentionally does not automate ---
echo ""
echo "Promoted v$VERSION (from $RC_TAG)."
echo ""
echo "Manual follow-ups:"
echo "  1. Forward-port any RC fixes from $RELEASE_BRANCH to main if main has diverged:"
echo "       git checkout main"
echo "       git log main..$RELEASE_BRANCH --oneline    # see what's on the branch but not on main"
echo "       # cherry-pick or merge as appropriate"
echo ""
echo "  2. Run the post-release doc review (issue filed in milestone $NEXT_MILESTONE)."
echo ""
echo "(The managed \`forge\` launcher still points at the last RC venv, whose"
echo " code matches v$VERSION. To track the final tag specifically, cut a"
echo " fresh venv from v$VERSION and re-link the managed launcher.)"
