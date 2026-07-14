#!/usr/bin/env bash
# scripts/apply-branch-protection.sh — apply branch protection to a release
# branch so GitHub's enablePullRequestAutoMerge mutation works and CI is
# required before a merge.
#
# Usage: scripts/apply-branch-protection.sh [--dry-run] <repo> <branch>
#
# Behavior:
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
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        *) ARGS+=("$arg") ;;
    esac
done

if [[ "${#ARGS[@]}" -ne 2 ]]; then
    echo "Usage: $0 [--dry-run] <repo> <branch>" >&2
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
# its `gate` job runs a matrix over python-version ["3.11","3.12","3.13"],
# yielding checks "gate (3.11)", "gate (3.12)", "gate (3.13)". If the ci.yml
# job name or the python-version matrix changes, update GATE_CONTEXTS to match
# or auto-merge arming will silently break again.
GATE_CONTEXTS='["gate (3.11)","gate (3.12)","gate (3.13)"]'
PROTECTION_BODY='{"required_status_checks":{"strict":false,"contexts":'"$GATE_CONTEXTS"'},"enforce_admins":null,"required_pull_request_reviews":null,"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}'

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
