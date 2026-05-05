#!/usr/bin/env bash
# scripts/cut-rc.sh — cut a TheForge release candidate
#
# Usage: scripts/cut-rc.sh [--dry-run] [--no-install] VERSION [RC_NUM]
#   VERSION   The target final version, e.g. 0.10.0
#   RC_NUM    The RC number, default 0 (so first RC is 0.10.0rc0)
#
# Cuts release/vX.Y from current main (or fast-forwards if it exists),
# bumps pyproject.toml to X.Y.ZrcN, runs gate, tags vX.Y.ZrcN, pushes
# branch and tag. Then installs the RC into the active Python environment
# so dogfood sprints exercise the candidate (use --no-install to skip).
#
# Does NOT block on open milestone issues — that's a promote-rc requirement.
# Prints them informationally so the operator can see what is or isn't in
# the cut.
#
# See RELEASING.md for the end-to-end RC flow.

set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

DRY_RUN=false
NO_INSTALL=false
VERSION=""
RC_NUM=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --no-install) NO_INSTALL=true ;;
        *)
            if [[ -z "$VERSION" ]]; then
                VERSION="$arg"
            elif [[ -z "$RC_NUM" ]]; then
                RC_NUM="$arg"
            else
                echo "Error: unexpected argument: $arg" >&2
                exit 2
            fi
            ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    echo "Error: VERSION required (e.g. 0.10.0)" >&2
    echo "Usage: scripts/cut-rc.sh [--dry-run] [--no-install] VERSION [RC_NUM]" >&2
    exit 2
fi

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: VERSION must be X.Y.Z (got: $VERSION)" >&2
    exit 2
fi

RC_NUM="${RC_NUM:-0}"
if [[ ! "$RC_NUM" =~ ^[0-9]+$ ]]; then
    echo "Error: RC_NUM must be a non-negative integer (got: $RC_NUM)" >&2
    exit 2
fi

RC_VERSION="${VERSION}rc${RC_NUM}"
RC_TAG="v${RC_VERSION}"
RELEASE_BRANCH="release/v$(echo "$VERSION" | cut -d. -f1,2)"

# CURRENT_VERSION is read after the release branch is checked out (step 4)
# to avoid bumping against a stale main version when the release branch is
# already at a prior RC.
CURRENT_VERSION=""

echo "Cutting RC      : $RC_VERSION"
echo "RC tag          : $RC_TAG"
echo "Release branch  : $RELEASE_BRANCH"
echo "Dry run         : $DRY_RUN"
echo "Install RC      : $([ "$NO_INSTALL" = true ] && echo "no (--no-install)" || echo "yes")"
echo ""

run() {
    echo "+ $*"
    if [[ "$DRY_RUN" == false ]]; then
        "$@"
    fi
}

# --- 1. Print milestone state (informational, do not block) ---
echo "==> Milestone v$VERSION state (informational)..."
OPEN_ISSUES=$(gh issue list --repo fuzzypete/theforge --milestone "v$VERSION" --state open --json number --jq 'length')
echo "    Open issues remaining in milestone v$VERSION: $OPEN_ISSUES"
if [[ "$OPEN_ISSUES" != "0" ]]; then
    echo "    (RC cuts do not require a clean milestone; promote-rc.sh does.)"
    gh issue list --repo fuzzypete/theforge --milestone "v$VERSION" --state open
fi
echo ""

# --- 2. Verify clean state and pull main ---
echo "==> Verifying clean state..."
if [[ -n "$(git status --porcelain)" ]]; then
    echo "Error: working tree is dirty. Commit or stash changes before cutting an RC." >&2
    exit 1
fi
run git checkout main
run git pull --ff-only

# --- 3. Create or fast-forward the release branch ---
echo "==> Preparing release branch $RELEASE_BRANCH..."
if git show-ref --verify --quiet "refs/heads/$RELEASE_BRANCH"; then
    run git checkout "$RELEASE_BRANCH"
    if git show-ref --verify --quiet "refs/remotes/origin/$RELEASE_BRANCH"; then
        run git pull --ff-only origin "$RELEASE_BRANCH"
    fi
elif git show-ref --verify --quiet "refs/remotes/origin/$RELEASE_BRANCH"; then
    run git checkout -b "$RELEASE_BRANCH" "origin/$RELEASE_BRANCH"
else
    run git checkout -b "$RELEASE_BRANCH"
    echo "    (created $RELEASE_BRANCH from main)"
fi

# --- 4. Refuse if the RC tag already exists ---
if git rev-parse --verify --quiet "$RC_TAG" >/dev/null; then
    echo "Error: tag $RC_TAG already exists locally. Delete it or bump RC_NUM." >&2
    exit 1
fi
if git ls-remote --tags origin "refs/tags/$RC_TAG" | grep -q "$RC_TAG"; then
    echo "Error: tag $RC_TAG already exists on origin. Bump RC_NUM." >&2
    exit 1
fi

# --- 5. Read current pyproject version (now that release branch is up to date) ---
CURRENT_VERSION=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
echo "==> Current version on $RELEASE_BRANCH: $CURRENT_VERSION"
if [[ "$CURRENT_VERSION" == "$RC_VERSION" ]]; then
    echo "Error: pyproject is already at $RC_VERSION on $RELEASE_BRANCH." >&2
    exit 1
fi

# --- 6. Bump pyproject to RC version ---
echo "==> Bumping pyproject.toml to $RC_VERSION..."
if [[ "$DRY_RUN" == false ]]; then
    sed -i '' "s/^version = \"$CURRENT_VERSION\"/version = \"$RC_VERSION\"/" pyproject.toml
    BUMPED=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
    if [[ "$BUMPED" != "$RC_VERSION" ]]; then
        echo "Error: pyproject bump failed — version is $BUMPED, expected $RC_VERSION." >&2
        exit 1
    fi
fi

# --- 7. Gate ---
echo "==> Running gate..."
run make gate

# --- 8. Commit, tag, push ---
echo "==> Committing, tagging, pushing..."
run git add pyproject.toml
run git commit -m "chore: cut $RC_TAG"
run git tag "$RC_TAG"
run git push -u origin "$RELEASE_BRANCH"
run git push origin "$RC_TAG"

# --- 9. Configure branch protection on release branch so auto-merge works ---
#
# GitHub's enablePullRequestAutoMerge mutation (which Forge calls after APPROVE
# via `gh pr merge --auto`) requires the target branch to have a branch
# protection rule. Without protection, every RC-ladder fix lands in
# MERGE_FAILED and the operator click-merges by hand. The helper applies a
# minimal protection ruleset — just enough to make GitHub treat the branch
# as protected. Specific rule selection is intentionally minimal; tightening
# is a follow-on.
echo "==> Configuring branch protection on $RELEASE_BRANCH..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTECT_ARGS=()
[[ "$DRY_RUN" == true ]] && PROTECT_ARGS+=("--dry-run")
PROTECT_ARGS+=("fuzzypete/theforge" "$RELEASE_BRANCH")
"$SCRIPT_DIR/apply-branch-protection.sh" "${PROTECT_ARGS[@]}" || true

# --- 10. Install the RC into the active env, then assert the right binary is on PATH ---
if [[ "$NO_INSTALL" == false ]]; then
    echo "==> Installing $RC_TAG into active Python environment..."
    run pip install --force-reinstall "git+https://github.com/fuzzypete/theforge.git@${RC_TAG}"
    if [[ "$DRY_RUN" == false ]]; then
        FORGE_PATH=$(command -v forge 2>/dev/null || echo "<not found>")
        PIP_PATH=$(command -v pip 2>/dev/null || echo "<not found>")
        PYTHON_PATH=$(command -v python 2>/dev/null || echo "<not found>")
        PROBE_OUTPUT=$(forge version 2>&1)
        PROBE_EXIT=$?
        INSTALLED_VERSION=$(echo "$PROBE_OUTPUT" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*' | head -1)
        echo "    forge version    : $PROBE_OUTPUT"
        echo "    forge on PATH    : $FORGE_PATH"
        echo "    pip on PATH      : $PIP_PATH"
        echo "    python on PATH   : $PYTHON_PATH"
        if [[ $PROBE_EXIT -ne 0 ]]; then
            echo "" >&2
            echo "Error: 'forge version' failed (exit $PROBE_EXIT)." >&2
            echo "       forge on PATH: $FORGE_PATH" >&2
            echo "       Probe output: $PROBE_OUTPUT" >&2
            exit 1
        elif [[ "$INSTALLED_VERSION" != *"$RC_VERSION"* ]]; then
            echo "" >&2
            echo "Error: installed forge version does not match RC ($RC_VERSION)." >&2
            echo "       'pip install' may have targeted a different environment than the 'forge' on PATH." >&2
            echo "       Compare the pip/python/forge paths above and reinstall into the correct env, e.g.:" >&2
            echo "         \"\$PYTHON_PATH\" -m pip install --force-reinstall git+https://github.com/fuzzypete/theforge.git@${RC_TAG}" >&2
            exit 1
        fi
    fi
else
    echo "==> Skipping install (--no-install)."
    echo "    To install manually:"
    echo "      pip install --force-reinstall git+https://github.com/fuzzypete/theforge.git@${RC_TAG}"
fi

# --- 11. Print test ladder ---
echo ""
echo "==> $RC_TAG cut on $RELEASE_BRANCH."
echo ""
echo "Test ladder — run on TheForge's own repo against the installed RC:"
echo "  1. Smoke pass (small story):       forge sprint --verbose --issues <small-issue>  --budget 50 --parallel 1"
echo "  2. Boundary pass (medium story):   forge sprint --verbose --issues <medium-issue> --budget 50 --parallel 1"
echo "  3. Moneyshot pass (high-complexity story): forge sprint --verbose --issues <high-complexity-issue> --budget 50 --parallel 1"
echo ""
echo "Pick the issues from milestone v$(echo "$VERSION" | awk -F. '{print $1"."$2+1".0"}') (or the next milestone after v$VERSION)."
echo "Watch for: 'budget' wording in audit/logs (should be 'per-story routing cost cap'), silent tier/pool downgrades without terminal warnings, regressions in any v$VERSION-shipped behavior."
echo ""
echo "If the ladder passes, promote with:"
echo "  scripts/promote-rc.sh $VERSION"
echo ""
echo "If the ladder fails, fix on $RELEASE_BRANCH and cut another RC:"
echo "  scripts/cut-rc.sh $VERSION $((RC_NUM + 1))"
