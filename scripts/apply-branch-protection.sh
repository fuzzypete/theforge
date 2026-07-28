#!/usr/bin/env bash
# scripts/apply-branch-protection.sh — apply branch protection to a release
# branch so GitHub's enablePullRequestAutoMerge mutation works and CI is
# required before a merge.
#
# Usage: scripts/apply-branch-protection.sh [--dry-run] [--force] <repo> <branch>
#
# Behavior:
#   - Refuse any branch outside release/* unless --force is given. The PUT
#     below is a WHOLESALE replacement shaped for release branches; pointed at
#     a branch with richer protection it silently deletes the rules it does not
#     name. See the guard and the PROTECTION_BODY note for the full rationale.
#   - Always PUT the protection ruleset — this brings the branch up to the
#     required state whether or not it was already protected. (An earlier
#     revision preserved existing protection untouched; that left branches
#     stuck in a half-protected state with no required checks, so we now
#     re-apply unconditionally.)
#   - On API failure, warn (do NOT abort with non-zero) so the caller can
#     proceed; manual recovery command is printed to stderr.
#   - With --dry-run, log the planned PUT and body but do not call gh.
#
# Extracted from cut-rc.sh so the behavior can be exercised by tests with
# a PATH-mocked `gh`.

set -uo pipefail

DRY_RUN=false
FORCE=false
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --force) FORCE=true ;;
        *) ARGS+=("$arg") ;;
    esac
done

if [[ "${#ARGS[@]}" -ne 2 ]]; then
    echo "Usage: $0 [--dry-run] [--force] <repo> <branch>" >&2
    exit 2
fi

REPO="${ARGS[0]}"
BRANCH="${ARGS[1]}"

# required_status_checks.contexts must name the CI checks that must pass before
# a merge. Two reasons this must be non-empty (an empty [] fails both):
#   1. Auto-merge arming: GitHub's enablePullRequestAutoMerge mutation needs
#      something to wait on. With zero required checks (and no required
#      reviews) there is nothing gating the merge, so GitHub refuses to arm
#      auto-merge with MERGE_ARMING_FAILED. Confirmed 2026-07-13: the #1441
#      sprint on release/v0.11 approved its PR (#1658) yet failed arming
#      because the protection body carried empty contexts.
#   2. CI enforcement: with empty required contexts a PR can merge into the
#      release branch with a red gate — a protection hole on the hardened line.
#
# These context names are the check names produced by .github/workflows/ci.yml:
# its `gate` job runs a matrix over python-version ["3.12"], yielding the check
# "gate (3.12)". If the ci.yml job name or the python-version matrix changes,
# update GATE_CONTEXTS to match or auto-merge arming will silently break again.
# Requiring a context the matrix does not produce leaves every PR waiting on a
# status that never arrives; producing a context that is not required leaves a
# merge ungated. Change both files together and re-apply protection.
GATE_CONTEXTS='["gate (3.12)"]'
# This body is the COMPLETE ruleset for a release branch: the PUT replaces
# protection wholesale, so every rule not named here is cleared. Note
# required_pull_request_reviews:null in particular — it disables "require a
# pull request before merging". The guard below is what keeps that from
# reaching a branch where it would be destructive.
PROTECTION_BODY='{"required_status_checks":{"strict":false,"contexts":'"$GATE_CONTEXTS"'},"enforce_admins":null,"required_pull_request_reviews":null,"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}'

# Mechanical guard: this ruleset is only safe on release/* branches.
#
# Release branches are already exactly this shape — cut-rc.sh has been applying
# this same body to them — so the wholesale PUT is faithful there. Other
# branches are not: main carries a required_pull_request_reviews block, and
# PUTting this body at it silently disables "require a pull request before
# merging" on the default branch. No error, no diff, nothing to notice.
#
# This is not hypothetical. During the #2012 sprint a review instructed "run
# apply-branch-protection.sh for each protected branch"; following that
# literally would have stripped main's PR requirement. A comment saying so was
# already present and did not prevent it, so the constraint is enforced here
# instead of described.
#
# Guarded before the --dry-run branch so a preview reports the refusal too,
# rather than printing a PUT that a real run would reject. cut-rc.sh always
# passes release/v<major>.<minor>, so this can never fire on the RC path.
#
# --force covers the deliberate case: bringing a genuinely new release-line
# branch up to this shape. To change ONLY the required contexts on some other
# branch, use the granular endpoint named in the refusal message instead.
if [[ "$FORCE" != true && "$BRANCH" != release/* ]]; then
    echo "[forge] ✗ refusing to apply the release-branch ruleset to '$BRANCH'" >&2
    echo "[forge]   This PUT replaces protection wholesale and would clear every" >&2
    echo "[forge]   rule it does not name (notably required_pull_request_reviews," >&2
    echo "[forge]   i.e. 'require a pull request before merging')." >&2
    echo "[forge]   To change ONLY the required checks, use the granular endpoint:" >&2
    echo "[forge]     echo '{\"strict\":false,\"contexts\":$GATE_CONTEXTS}' | gh api --method PATCH \\" >&2
    echo "[forge]       repos/$REPO/branches/$BRANCH/protection/required_status_checks --input -" >&2
    echo "[forge]   Re-run with --force if replacing the whole ruleset is intended." >&2
    exit 2
fi

if [[ "$DRY_RUN" == true ]]; then
    echo "+ (dry-run) gh api --method PUT repos/$REPO/branches/$BRANCH/protection --input -"
    echo "    body: $PROTECTION_BODY"
    echo "[forge] dry-run: would apply branch protection to $BRANCH (allow_auto_merge=true)"
    exit 0
fi

echo "[forge] applying branch protection to $BRANCH: allow_auto_merge=true"
if echo "$PROTECTION_BODY" | gh api --method PUT "repos/$REPO/branches/$BRANCH/protection" --input - >/dev/null 2>&1; then
    echo "[forge] ✓ branch protected; auto-merge enabled"
    exit 0
fi

echo "[forge] ⚠ failed to apply branch protection on $BRANCH (continuing)" >&2
echo "[forge]   apply manually: echo '$PROTECTION_BODY' | gh api --method PUT repos/$REPO/branches/$BRANCH/protection --input -" >&2
exit 0
