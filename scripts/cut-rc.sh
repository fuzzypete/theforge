#!/usr/bin/env bash
# scripts/cut-rc.sh — cut a TheForge release candidate
#
# Usage: scripts/cut-rc.sh [--dry-run] [--no-install] VERSION [RC_NUM]
#   VERSION   The target final version, e.g. 0.10.0
#   RC_NUM    The RC number, default 0 (so first RC is 0.10.0rc0)
#
# Cuts release/vX.Y from current main (or fast-forwards if it exists),
# bumps pyproject.toml to X.Y.ZrcN, runs gate, tags vX.Y.ZrcN, pushes
# branch and tag. Then installs the RC into an ISOLATED Python venv under
# .forge/rc-envs/v<X.Y.Z>rc<N>/ so dogfood sprints exercise the candidate
# without mutating any other Python environment (use --no-install to skip
# the verification install entirely).
#
# After the isolated venv install succeeds, the script repoints the
# operator's managed `forge` launcher (default: ~/.local/bin/forge,
# overridable via FORGE_MANAGED_LAUNCHER) at the new RC venv's binary, so
# plain `forge --version` reports the just-cut RC with no shell-config
# change. The launcher is a symlink — no Python environment is mutated, so
# editable installs and source-tree edits cannot silently change what
# plain `forge` runs. If plain `forge` currently resolves to a launcher
# inside a Python environment's bin/ (an editable-install console script,
# a pipx shim, etc.), the script refuses to overwrite it and prints
# guidance, since pip would later regenerate that file and break the
# binding.
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

# --- 9. Install the RC into an ISOLATED venv and verify against THAT venv's binary ---
#
# Earlier versions of this script ran `pip install --force-reinstall` against
# whatever Python env was active, which silently overwrote the operator's
# editable install of source. The cut process never installs into any
# pre-existing Python environment — the RC lives in a fresh venv, and the
# operator-facing `forge` command is repointed at it via a symlink (step 10).
RC_ENV_DIR="$(git rev-parse --show-toplevel)/.forge/rc-envs/${RC_TAG}"
RC_ENV_FORGE="${RC_ENV_DIR}/bin/forge"
RC_ENV_PIP="${RC_ENV_DIR}/bin/pip"
RC_ENV_PYTHON="${RC_ENV_DIR}/bin/python"

MANAGED_LAUNCHER="${FORGE_MANAGED_LAUNCHER:-${HOME}/.local/bin/forge}"
MANAGED_LAUNCHER_DIR="$(dirname "$MANAGED_LAUNCHER")"

if [[ "$NO_INSTALL" == false ]]; then
    echo "==> Verifying $RC_TAG installs cleanly (isolated venv at $RC_ENV_DIR)..."
    if [[ "$DRY_RUN" == false ]]; then
        if [[ -d "$RC_ENV_DIR" ]]; then
            rm -rf "$RC_ENV_DIR"
        fi
        mkdir -p "$(dirname "$RC_ENV_DIR")"
    fi
    run python3 -m venv "$RC_ENV_DIR"
    run "$RC_ENV_PIP" install --upgrade pip
    run "$RC_ENV_PIP" install "git+https://github.com/fuzzypete/theforge.git@${RC_TAG}"
    if [[ "$DRY_RUN" == false ]]; then
        INSTALLED_VERSION=$("$RC_ENV_FORGE" --version 2>/dev/null || echo "")
        echo "    isolated forge   : $RC_ENV_FORGE"
        echo "    forge --version  : $INSTALLED_VERSION"
        echo "    isolated pip     : $RC_ENV_PIP"
        echo "    isolated python  : $RC_ENV_PYTHON"
        if [[ "$INSTALLED_VERSION" != *"$RC_VERSION"* ]]; then
            echo "" >&2
            echo "Error: isolated forge version does not match RC ($RC_VERSION)." >&2
            echo "       Got: '$INSTALLED_VERSION'" >&2
            echo "       The cut tag may not be publishable; investigate before promoting." >&2
            exit 1
        fi
        echo "    ✓ $RC_TAG installed into isolated venv."
    fi

    # --- 10. Repoint the operator's managed `forge` launcher at the cut RC ---
    #
    # The cut establishes plain `forge` as the just-cut RC by atomically
    # swapping a forge-managed symlink. We refuse to overwrite a launcher
    # that lives inside a Python environment's bin/ (editable install,
    # pipx shim, etc.), since pip would later regenerate that file and
    # break the binding silently.
    echo "==> Repointing plain \`forge\` at $RC_TAG ($MANAGED_LAUNCHER -> $RC_ENV_FORGE)..."
    if [[ "$DRY_RUN" == false ]]; then
        CURRENT_FORGE="$(command -v forge 2>/dev/null || true)"
        if [[ -n "$CURRENT_FORGE" && "$CURRENT_FORGE" != "$MANAGED_LAUNCHER" ]]; then
            CURRENT_FORGE_DIR="$(dirname "$CURRENT_FORGE")"
            if [[ -f "${CURRENT_FORGE_DIR}/activate" \
                  || -f "${CURRENT_FORGE_DIR}/../pyvenv.cfg" \
                  || -f "${CURRENT_FORGE_DIR}/activate.fish" ]]; then
                echo "" >&2
                echo "Error: plain \`forge\` currently resolves to" >&2
                echo "         $CURRENT_FORGE" >&2
                echo "       which lives inside a Python environment's bin/." >&2
                echo "       Replacing it would clobber a pip-generated console" >&2
                echo "       script (e.g. an editable install of source); a later" >&2
                echo "       \`pip install -e .\` in that environment would regenerate" >&2
                echo "       the launcher and silently break the cut-RC binding." >&2
                echo "" >&2
                echo "       The cut RC is installed and verified at:" >&2
                echo "         $RC_ENV_FORGE" >&2
                echo "" >&2
                echo "       To make plain \`forge\` track cut RCs, ensure" >&2
                echo "         $MANAGED_LAUNCHER_DIR" >&2
                echo "       is on PATH ahead of any Python environment's bin/" >&2
                echo "       (deactivate the venv or reorder PATH), then re-run:" >&2
                echo "         scripts/cut-rc.sh $VERSION $RC_NUM" >&2
                exit 1
            fi
        fi

        mkdir -p "$MANAGED_LAUNCHER_DIR"
        ln -snf "$RC_ENV_FORGE" "$MANAGED_LAUNCHER"
        hash -r 2>/dev/null || true

        RESOLVED_FORGE="$(command -v forge 2>/dev/null || true)"
        if [[ "$RESOLVED_FORGE" != "$MANAGED_LAUNCHER" ]]; then
            echo "" >&2
            echo "Error: after repointing, plain \`forge\` resolves to" >&2
            echo "         '${RESOLVED_FORGE:-<not found>}'" >&2
            echo "       expected '$MANAGED_LAUNCHER'." >&2
            echo "       Ensure $MANAGED_LAUNCHER_DIR is on PATH ahead of any" >&2
            echo "       Python environment's bin/, then re-run scripts/cut-rc.sh." >&2
            exit 1
        fi

        PLAIN_VERSION=$(forge --version 2>/dev/null || echo "")
        if [[ "$PLAIN_VERSION" != *"$RC_VERSION"* ]]; then
            echo "" >&2
            echo "Error: plain \`forge --version\` reports '$PLAIN_VERSION'," >&2
            echo "       expected to contain $RC_VERSION." >&2
            echo "       The managed launcher symlink may be broken; investigate" >&2
            echo "         $MANAGED_LAUNCHER" >&2
            echo "         $RC_ENV_FORGE" >&2
            exit 1
        fi

        echo "    managed launcher : $MANAGED_LAUNCHER -> $RC_ENV_FORGE"
        echo "    forge --version  : $PLAIN_VERSION"
        echo "    ✓ plain \`forge\` now tracks $RC_TAG; substrate is isolated."
    fi
else
    echo "==> Skipping verification install (--no-install)."
    echo "    To verify manually in an isolated venv:"
    echo "      python3 -m venv \"$RC_ENV_DIR\""
    echo "      \"$RC_ENV_PIP\" install git+https://github.com/fuzzypete/theforge.git@${RC_TAG}"
    echo "      \"$RC_ENV_FORGE\" --version"
    echo "      ln -snf \"$RC_ENV_FORGE\" \"$MANAGED_LAUNCHER\""
fi

# --- 11. Print test ladder ---
echo ""
echo "==> $RC_TAG cut on $RELEASE_BRANCH."
echo ""
echo "Test ladder — run on TheForge's own repo against the cut RC."
echo "Plain \`forge\` now resolves to the cut RC via the managed launcher"
echo "($MANAGED_LAUNCHER -> $RC_ENV_FORGE); the substrate is isolated from"
echo "source-tree edits and editable installs."
echo ""
echo "  1. Smoke pass (small story):              forge sprint --verbose --issues <small-issue>  --budget 50 --parallel 1"
echo "  2. Boundary pass (medium story):          forge sprint --verbose --issues <medium-issue> --budget 50 --parallel 1"
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
