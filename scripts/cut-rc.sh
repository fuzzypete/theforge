#!/usr/bin/env bash
# scripts/cut-rc.sh — cut a TheForge release candidate
#
# Usage: scripts/cut-rc.sh [--dry-run] [--no-install] [--resume] VERSION [RC_NUM]
#   VERSION   The target final version, e.g. 0.10.0
#   RC_NUM    The RC number, default 0 (so first RC is 0.10.0rc0)
#   --resume  Re-enter a previous cut past the tag-push step. The post-tag
#             steps (isolated venv install, launcher repoint, ladder print)
#             must remain runnable after the operator has fixed whatever
#             env-state issue caused the original cut to abort. With
#             --resume, the script skips the bump/commit/tag/push phase
#             (requiring the tag to already exist on origin) and picks up
#             at the isolated-venv install.
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
# plain `forge version` reports the just-cut RC with no shell-config
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

# --- Self-isolate from the on-disk script source ---------------------------
# This script performs `git checkout` mid-run (steps 2-3). Bash reads its
# script by byte offset from the on-disk file, so a checkout that swaps in a
# divergent copy of scripts/cut-rc.sh causes bash to continue executing
# whatever bytes land at the current offset in the new file — silent
# corruption per byte, per branch. To make behavior independent of which
# branch the operator launched from, copy the launching source to a temp
# path once, before any git operation runs, and re-exec from there. The
# child's $CUT_RC_SELF_COPY guard prevents infinite re-exec; the trap in
# the child removes the temp file on exit.
if [[ "${CUT_RC_SELF_COPY:-}" != "1" ]]; then
    _self_copy="$(mktemp -t cut-rc.XXXXXX)"
    cat "$0" > "$_self_copy"
    chmod +x "$_self_copy"
    export CUT_RC_SELF_COPY=1
    export CUT_RC_SELF_COPY_PATH="$_self_copy"
    exec bash "$_self_copy" "$@"
fi
if [[ -n "${CUT_RC_SELF_COPY_PATH:-}" ]]; then
    trap 'rm -f "$CUT_RC_SELF_COPY_PATH"' EXIT
fi

export PATH="/opt/homebrew/bin:$PATH"

DRY_RUN=false
NO_INSTALL=false
RESUME=false
VERSION=""
RC_NUM=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --no-install) NO_INSTALL=true ;;
        --resume) RESUME=true ;;
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
    echo "Usage: scripts/cut-rc.sh [--dry-run] [--no-install] [--resume] VERSION [RC_NUM]" >&2
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
echo "Resume          : $RESUME"
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

if [[ "$RESUME" == false ]]; then
    # --- 4. Refuse if the RC tag already exists ---
    if git rev-parse --verify --quiet "$RC_TAG" >/dev/null; then
        echo "Error: tag $RC_TAG already exists locally. Delete it or bump RC_NUM." >&2
        echo "       If you are recovering from a previous cut that failed after" >&2
        echo "       tag-push, re-run with --resume:" >&2
        echo "         scripts/cut-rc.sh --resume $VERSION $RC_NUM" >&2
        exit 1
    fi
    if git ls-remote --tags origin "refs/tags/$RC_TAG" | grep -q "$RC_TAG"; then
        echo "Error: tag $RC_TAG already exists on origin. Bump RC_NUM." >&2
        echo "       If you are recovering from a previous cut that failed after" >&2
        echo "       tag-push, re-run with --resume:" >&2
        echo "         scripts/cut-rc.sh --resume $VERSION $RC_NUM" >&2
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
else
    # --- 4-8 (resume). Skip bump/commit/tag/push; the previous cut already
    # pushed the tag. Verify that's actually the case so --resume isn't used
    # for a cut that never reached tag-push.
    echo "==> --resume: skipping bump/commit/tag/push (must already be on origin)..."
    if ! git ls-remote --tags origin "refs/tags/$RC_TAG" | grep -q "$RC_TAG"; then
        echo "Error: --resume requires $RC_TAG to already exist on origin." >&2
        echo "       It does not. Either run without --resume to cut it fresh," >&2
        echo "       or correct VERSION / RC_NUM." >&2
        exit 1
    fi
    CURRENT_VERSION=$(grep '^version' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
    if [[ "$CURRENT_VERSION" != "$RC_VERSION" ]]; then
        echo "    warn: pyproject on $RELEASE_BRANCH is at $CURRENT_VERSION," >&2
        echo "          expected $RC_VERSION. The tag is on origin so resume" >&2
        echo "          will install from there; only the local working tree" >&2
        echo "          mismatches." >&2
    fi
fi

# --- 8b. Configure branch protection on the release branch so auto-merge works ---
#
# GitHub's enablePullRequestAutoMerge mutation (which Forge calls after APPROVE
# via `gh pr merge --auto`) requires the target branch to have a branch
# protection rule. Without protection, every RC-ladder fix lands in
# MERGE_FAILED and the operator click-merges by hand. The helper applies a
# minimal protection ruleset; it is idempotent and safe to re-run on --resume.
echo "==> Configuring branch protection on $RELEASE_BRANCH..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTECT_ARGS=()
[[ "$DRY_RUN" == true ]] && PROTECT_ARGS+=("--dry-run")
PROTECT_ARGS+=("fuzzypete/theforge" "$RELEASE_BRANCH")
"$SCRIPT_DIR/apply-branch-protection.sh" "${PROTECT_ARGS[@]}" || true

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
    run "$RC_ENV_PIP" install "theforge[all] @ git+https://github.com/fuzzypete/theforge.git@${RC_TAG}"
    if [[ "$DRY_RUN" == false ]]; then
        INSTALLED_OUTPUT=$("$RC_ENV_FORGE" version 2>&1 || echo "")
        INSTALLED_VERSION=$(echo "$INSTALLED_OUTPUT" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*' | head -1)
        echo "    isolated forge   : $RC_ENV_FORGE"
        echo "    forge version    : $INSTALLED_VERSION"
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
    # swapping a forge-managed symlink at $MANAGED_LAUNCHER. If plain
    # `forge` currently resolves into a Python environment's bin/, the
    # symlink itself doesn't overwrite that file — but we still want to
    # reason about what we're displacing on PATH, since a pip-managed
    # console script for an unrelated package would later regenerate
    # itself and silently shadow the symlink.
    #
    # Three-way discrimination on the existing in-venv launcher:
    #   - Same RC as we're cutting: clobber is identity, proceed silently.
    #   - Different theforge version: print one-line note (the operator's
    #     env install is still importable as `python -m theforge`).
    #   - Unrecognized (no importable theforge, or probe failed): refuse,
    #     since we cannot reason about what we're displacing.
    echo "==> Repointing plain \`forge\` at $RC_TAG ($MANAGED_LAUNCHER -> $RC_ENV_FORGE)..."
    if [[ "$DRY_RUN" == false ]]; then
        # Populated by the in-venv probe below when CURRENT_FORGE lives in a
        # Python env with an importable theforge distribution. The post-repoint
        # validation below uses this to tolerate a still-shadowing env launcher
        # whose contents we've already vetted: command -v forge may continue
        # to return the env binary instead of the managed symlink, but that's
        # by-design when the env bin precedes ~/.local/bin on PATH and the env
        # has theforge installed.
        INVENV_LAUNCHER_VERSION=""
        INVENV_LAUNCHER_DIR=""
        CURRENT_FORGE="$(command -v forge 2>/dev/null || true)"
        if [[ -n "$CURRENT_FORGE" && "$CURRENT_FORGE" != "$MANAGED_LAUNCHER" ]]; then
            PYENV_ROOT="${PYENV_ROOT:-${HOME}/.pyenv}"
            if [[ "$CURRENT_FORGE" == "${PYENV_ROOT}/shims/forge" ]] \
               && command -v pyenv >/dev/null 2>&1; then
                PYENV_FORGE="$(pyenv which forge 2>/dev/null || true)"
                if [[ -n "$PYENV_FORGE" && "$PYENV_FORGE" != "$CURRENT_FORGE" ]]; then
                    CURRENT_FORGE="$PYENV_FORGE"
                fi
            fi
            CURRENT_FORGE_DIR="$(dirname "$CURRENT_FORGE")"
            CURRENT_FORGE_IS_PYENV_VERSION=false
            if [[ "$CURRENT_FORGE" == "${PYENV_ROOT}/versions/"*/bin/forge ]]; then
                CURRENT_FORGE_IS_PYENV_VERSION=true
            fi
            if [[ -f "${CURRENT_FORGE_DIR}/activate" \
                  || -f "${CURRENT_FORGE_DIR}/../pyvenv.cfg" \
                  || -f "${CURRENT_FORGE_DIR}/activate.fish" \
                  || "$CURRENT_FORGE_IS_PYENV_VERSION" == true ]]; then
                CURRENT_FORGE_PY=""
                if [[ -x "${CURRENT_FORGE_DIR}/python" ]]; then
                    CURRENT_FORGE_PY="${CURRENT_FORGE_DIR}/python"
                elif [[ -x "${CURRENT_FORGE_DIR}/python3" ]]; then
                    CURRENT_FORGE_PY="${CURRENT_FORGE_DIR}/python3"
                fi
                INSTALLED_THEFORGE_VERSION=""
                if [[ -n "$CURRENT_FORGE_PY" ]]; then
                    INSTALLED_THEFORGE_VERSION="$("$CURRENT_FORGE_PY" -c 'import importlib.metadata as m; print(m.version("theforge"))' 2>/dev/null || true)"
                fi

                # An env can have theforge installed while CURRENT_FORGE itself
                # is a hand-written wrapper or a console script for a different
                # package. Identify CURRENT_FORGE as the theforge console
                # script by requiring an actual non-comment Python import
                # line — pyproject.toml has
                # [project.scripts] forge = "theforge.cli:main", so every
                # pip-generated console script contains a literal
                # `from theforge.cli import main` (or `import theforge`) at
                # column 0. Comments and incidental text (e.g. a shell
                # wrapper with `# copied from theforge`) must not satisfy
                # the check.
                CURRENT_FORGE_IS_THEFORGE=false
                if [[ -f "$CURRENT_FORGE" ]] \
                   && grep -q -E '^[[:space:]]*(from|import)[[:space:]]+theforge([[:space:].]|$)' "$CURRENT_FORGE" 2>/dev/null; then
                    CURRENT_FORGE_IS_THEFORGE=true
                fi

                if [[ -z "$INSTALLED_THEFORGE_VERSION" || "$CURRENT_FORGE_IS_THEFORGE" == false ]]; then
                    echo "" >&2
                    echo "Error: plain \`forge\` currently resolves to" >&2
                    echo "         $CURRENT_FORGE" >&2
                    echo "       which lives inside a Python environment's bin/," >&2
                    if [[ -z "$INSTALLED_THEFORGE_VERSION" ]]; then
                        echo "       but that environment has no importable \`theforge\`" >&2
                        echo "       distribution (probe via ${CURRENT_FORGE_PY:-<no python found in that bin/>}" >&2
                        echo "       returned no version)." >&2
                    else
                        echo "       and that environment has theforge==$INSTALLED_THEFORGE_VERSION" >&2
                        echo "       installed, but the launcher file itself does not look" >&2
                        echo "       like theforge's pip-generated console script (no" >&2
                        echo "       non-comment \`from theforge\` / \`import theforge\` line" >&2
                        echo "       at column 0; incidental comment text does not count)." >&2
                    fi
                    echo "       The script cannot reason about what \`forge\` actually" >&2
                    echo "       is — it may be a console script for an unrelated" >&2
                    echo "       package, a hand-written wrapper, or an editable install" >&2
                    echo "       of a different repo. Refusing to proceed rather than" >&2
                    echo "       silently displace it on PATH." >&2
                    echo "" >&2
                    echo "       The cut RC is installed and verified at:" >&2
                    echo "         $RC_ENV_FORGE" >&2
                    echo "" >&2
                    echo "       If you want plain \`forge\` to track cut RCs, ensure" >&2
                    echo "         $MANAGED_LAUNCHER_DIR" >&2
                    echo "       is on PATH ahead of $CURRENT_FORGE_DIR, then resume" >&2
                    echo "       this cut (the tag is already on origin):" >&2
                    echo "         scripts/cut-rc.sh --resume $VERSION $RC_NUM" >&2
                    exit 1
                elif [[ "$INSTALLED_THEFORGE_VERSION" == "$RC_VERSION" ]]; then
                    # Same RC as we're cutting; repoint is identity. Spec says
                    # this case completes silently — no note.
                    INVENV_LAUNCHER_VERSION="$INSTALLED_THEFORGE_VERSION"
                    INVENV_LAUNCHER_DIR="$CURRENT_FORGE_DIR"
                else
                    echo "    note: taking over plain \`forge\`; theforge==$INSTALLED_THEFORGE_VERSION"
                    echo "          in $CURRENT_FORGE_DIR is still importable as"
                    echo "          \`$CURRENT_FORGE_PY -m theforge\`."
                    INVENV_LAUNCHER_VERSION="$INSTALLED_THEFORGE_VERSION"
                    INVENV_LAUNCHER_DIR="$CURRENT_FORGE_DIR"
                fi
            fi
        fi

        mkdir -p "$MANAGED_LAUNCHER_DIR"
        ln -snf "$RC_ENV_FORGE" "$MANAGED_LAUNCHER"
        hash -r 2>/dev/null || true

        RESOLVED_FORGE="$(command -v forge 2>/dev/null || true)"
        if [[ "$RESOLVED_FORGE" != "$MANAGED_LAUNCHER" ]]; then
            if [[ -z "$INVENV_LAUNCHER_VERSION" ]]; then
                echo "" >&2
                echo "Error: after repointing, plain \`forge\` resolves to" >&2
                echo "         '${RESOLVED_FORGE:-<not found>}'" >&2
                echo "       expected '$MANAGED_LAUNCHER'." >&2
                echo "       Ensure $MANAGED_LAUNCHER_DIR is on PATH ahead of any" >&2
                echo "       Python environment's bin/, then resume this cut" >&2
                echo "       (the tag is already on origin):" >&2
                echo "         scripts/cut-rc.sh --resume $VERSION $RC_NUM" >&2
                exit 1
            fi
            # The in-venv probe vetted what plain `forge` resolves to, but
            # the RC cut contract requires plain `forge` itself to run the
            # just-cut candidate before we print the dogfood ladder below.
        fi

        PLAIN_OUTPUT=$(forge version 2>&1 || echo "")
        PLAIN_VERSION=$(echo "$PLAIN_OUTPUT" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*' | head -1)
        if [[ "$PLAIN_VERSION" != *"$RC_VERSION"* ]]; then
            if [[ -n "$INVENV_LAUNCHER_VERSION" \
                  && "$PLAIN_VERSION" == *"$INVENV_LAUNCHER_VERSION"* ]]; then
                echo "" >&2
                echo "Error: plain \`forge version\` reports '$PLAIN_VERSION'," >&2
                echo "       but the cut RC is $RC_VERSION." >&2
                echo "       The managed launcher symlink was installed:" >&2
                echo "         $MANAGED_LAUNCHER -> $RC_ENV_FORGE" >&2
                echo "       but it is shadowed by the Python environment at:" >&2
                echo "         $INVENV_LAUNCHER_DIR" >&2
                echo "" >&2
                echo "       Make plain \`forge\` track $RC_TAG by either reordering PATH" >&2
                echo "       so $MANAGED_LAUNCHER_DIR precedes $INVENV_LAUNCHER_DIR," >&2
                echo "       or \`pip install theforge==$RC_VERSION\` in that env, then resume" >&2
                echo "       this cut (the tag is already on origin):" >&2
                echo "         scripts/cut-rc.sh --resume $VERSION $RC_NUM" >&2
                exit 1
            else
                echo "" >&2
                echo "Error: plain \`forge version\` reports '$PLAIN_VERSION'," >&2
                echo "       expected to contain $RC_VERSION." >&2
                echo "       The managed launcher symlink may be broken; investigate" >&2
                echo "         $MANAGED_LAUNCHER" >&2
                echo "         $RC_ENV_FORGE" >&2
                exit 1
            fi
        else
            echo "    managed launcher : $MANAGED_LAUNCHER -> $RC_ENV_FORGE"
            echo "    forge version    : $PLAIN_VERSION"
            echo "    ✓ plain \`forge\` now tracks $RC_TAG; substrate is isolated."
        fi
    fi
else
    echo "==> Skipping verification install (--no-install)."
    echo "    To verify manually in an isolated venv:"
    echo "      python3 -m venv \"$RC_ENV_DIR\""
    echo "      \"$RC_ENV_PIP\" install \"theforge[all] @ git+https://github.com/fuzzypete/theforge.git@${RC_TAG}\""
    echo "      \"$RC_ENV_FORGE\" version"
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
