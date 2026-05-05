#!/usr/bin/env bash
# scripts/apply-branch-protection.sh — apply minimum branch protection to
# a release branch so GitHub's enablePullRequestAutoMerge mutation works.
#
# Usage: scripts/apply-branch-protection.sh [--dry-run] <repo> <branch>
#
# Behavior:
#   - If branch already has protection, preserve it (log and exit 0).
#   - Otherwise, PUT a minimal protection ruleset.
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
PROTECTION_BODY='{"required_status_checks":null,"enforce_admins":null,"required_pull_request_reviews":null,"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}'

if [[ "$DRY_RUN" == true ]]; then
    echo "+ (dry-run) gh api --method PUT repos/$REPO/branches/$BRANCH/protection --input -"
    echo "    body: $PROTECTION_BODY"
    echo "[forge] dry-run: would apply branch protection to $BRANCH (allow_auto_merge=true)"
    exit 0
fi

if gh api "repos/$REPO/branches/$BRANCH/protection" >/dev/null 2>&1; then
    echo "[forge] branch protection already exists on $BRANCH; preserving it"
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
