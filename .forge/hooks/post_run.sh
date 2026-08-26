#!/usr/bin/env bash
# Reference post_run hook: file GitHub Issues for P1/P2 findings
# Fires after every forge run; creates one issue per finding on ESCALATE or
# APPROVE-with-findings outcomes.
#
# Requires: gh CLI (authenticated), jq
set -euo pipefail

# Guard: gh not installed → warn and exit cleanly
if ! command -v gh &> /dev/null; then
  echo "[forge hook] gh CLI not found — skipping GitHub issue creation" >&2
  exit 0
fi

# Guard: jq not installed → warn and exit cleanly
if ! command -v jq &> /dev/null; then
  echo "[forge hook] jq not found — skipping GitHub issue creation" >&2
  exit 0
fi

# Every finding body is validated against the state this hook declares for it
# before it is filed, through the same shape specification the sprint gate
# reads.
FORGE_PRODUCER="post-run-hook-finding"
FORGE_DECLARED="needs_operator_action"

# Find an interpreter that can actually import theforge. The hook ships into
# repositories whose environment forge does not control, and the `python3` on
# PATH there is frequently not the one forge is installed into — so prefer the
# interpreter behind the installed `forge` executable over whatever PATH
# resolves first. FORGE_PYTHON overrides the search entirely.
forge_resolve_python() {
  local candidate forge_bin
  if [ -n "${FORGE_PYTHON:-}" ]; then
    printf '%s' "$FORGE_PYTHON"
    return 0
  fi
  forge_bin=$(command -v forge 2>/dev/null || true)
  if [ -n "$forge_bin" ]; then
    candidate=$(head -1 "$forge_bin" 2>/dev/null | sed -n 's|^#!\([^ ]*\).*|\1|p')
    if [ -n "$candidate" ] && "$candidate" -c 'import theforge' >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  fi
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import theforge' >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  # Nothing found. Report the default anyway so the failure surfaces as a
  # named validator error per finding rather than a silent skip.
  printf 'python3'
}

FORGE_PYTHON=$(forge_resolve_python)

# Validate one rendered finding body, read from stdin. Fail closed: ANY nonzero
# exit — a verdict mismatch, a missing interpreter, theforge not importable —
# means this finding is not filed. A hook that cannot check its own output must
# not write.
forge_validate_body() {
  "$FORGE_PYTHON" -m theforge.shape_check.producer \
    --producer "$FORGE_PRODUCER" \
    --declared "$FORGE_DECLARED" \
    --strict \
    --title "$1" \
    --body-stdin \
    --label "bug" \
    --label "forge-finding" \
    --label "needs-triage" \
    --label "$2"
}

payload=$(cat)

verdict=$(echo "$payload" | jq -r '.verdict')
slug=$(echo "$payload" | jq -r '.slug')
branch=$(echo "$payload" | jq -r '.branch')
branch_display="${branch//\// -> }"
summary=$(echo "$payload" | jq -r '.summary')
findings_count=$(echo "$payload" | jq '.findings | length')

# Ensure the intake label exists before filing findings. --force keeps the
# description aligned without failing when the label already exists.
gh label create "needs-triage" \
  --description "Forge finding awaiting explicit triage decision" \
  --color "D4C5F9" \
  --force >/dev/null 2>&1 || true

# Only act on ESCALATE or APPROVE outcomes (REQUEST_CHANGES = still in review)
if [ "$verdict" != "ESCALATE" ] && [ "$verdict" != "APPROVE" ]; then
  exit 0
fi

# File one GitHub Issue per finding (only when findings exist)
if [ "$findings_count" -gt 0 ]; then
  echo "$payload" | jq -c '.findings[]' | while read -r finding; do
    sev=$(echo "$finding" | jq -r '.severity')
    observed=$(echo "$finding" | jq -r '.observed // ""')
    expected=$(echo "$finding" | jq -r '.expected // ""')
    evidence=$(echo "$finding" | jq -r '.evidence // ""')
    suggestion=$(echo "$finding" | jq -r '.suggestion // ""')

    # Title: [P1] slug: <observed> (truncated to 72 chars)
    raw_title="[${sev}] ${slug}: ${observed}"
    title="${raw_title:0:72}"

    body="**Observed:** ${observed}

**Expected:** ${expected}

**Evidence:** ${evidence}"

    if [ -n "$suggestion" ]; then
      body="${body}

## Suggested approach (non-binding)

${suggestion}

*Non-binding guidance from the reviewer; the dev agent is free to pick a different fix.*"
    fi

    footer="*Filed by theforge post_run hook · story \`${slug}\`"
    footer="${footer} · branch \`${branch_display}\` · ${verdict}.*"
    body="${body}

---
${footer}"

    sev_label=$(echo "$sev" | tr 'A-Z' 'a-z')

    if printf '%s' "$body" | forge_validate_body "$title" "$sev_label" >&2; then
      gh issue create \
        --title "$title" \
        --body "$body" \
        --label "bug" \
        --label "forge-finding" \
        --label "needs-triage" \
        --label "$sev_label" || true
    else
      echo "[forge hook] not filing finding: producer $FORGE_PRODUCER could not \
confirm the body occupies its declared state ($FORGE_DECLARED). See the \
validator output above; set FORGE_PYTHON to an interpreter that can import \
theforge if the check could not run." >&2
    fi
  done
fi

# ── PR Review Attribution ─────────────────────────────────────────────
# Opt-in: set FORGE_GH_PR_REVIEWS=1 to enable posting per-reviewer GitHub reviews.
# The gh api calls below use `repos/{owner}/{repo}` — gh CLI resolves these
# automatically to the current repository when run from within the repo.
# See: https://cli.github.com/manual/gh_api (the {owner}/{repo} tokens are
# expanded by gh from the remote origin URL automatically).
[ -z "${FORGE_GH_PR_REVIEWS:-}" ] && exit 0

# Only post reviews on APPROVE
[ "$verdict" != "APPROVE" ] && exit 0

pr_number=$(echo "$payload" | jq -r '.pr_number // "null"')
[ "$pr_number" = "null" ] && exit 0

reviewers_count=$(echo "$payload" | jq '(.reviewers // []) | length')

if [ "$reviewers_count" -le 1 ]; then
  # Single-reviewer (or absent): post one APPROVE with that reviewer's findings
  reviewer_body=$(echo "$payload" | jq -r '
    (.reviewers // []) | if length == 0 then
      "Approved by theforge review pool.\n\n*Posted by theforge post_run hook.*"
    else
      .[0] as $r |
      "**Reviewer:** \($r.name) (`\($r.model)`)\n\n**Summary:** \($r.summary)\n\n" +
      (if ($r.findings | length) > 0 then
        "**Findings:**\n" +
        ($r.findings | map(
          "- [`\(.file):\(.line // "?")`] [\(.severity)] \(.observed // .description // "")"
        ) | join("\n")) + "\n\n"
      else "" end) +
      "*Posted by theforge post_run hook.*"
    end
  ')
  jq -n --arg body "$reviewer_body" --arg event "APPROVE" \
    '{body: $body, event: $event}' | \
    gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \
      --method POST \
      --input - || true
else
  # Multi-reviewer: post one COMMENT per reviewer, then one final APPROVE
  echo "$payload" | jq -c '.reviewers[]' | while read -r reviewer; do
    r_name=$(echo "$reviewer" | jq -r '.name')
    r_model=$(echo "$reviewer" | jq -r '.model')
    r_verdict=$(echo "$reviewer" | jq -r '.verdict')
    r_summary=$(echo "$reviewer" | jq -r '.summary')
    r_findings_count=$(echo "$reviewer" | jq '.findings | length')

    comment_body="**Reviewer:** ${r_name} (\`${r_model}\`)
**Verdict:** ${r_verdict}
**Summary:** ${r_summary}"

    if [ "$r_findings_count" -gt 0 ]; then
      findings_text=$(echo "$reviewer" | jq -r '
        .findings | map(
          "- [`\(.file):\(.line // "?")`] [\(.severity)] \(.observed // .description // "")"
        ) | join("\n")
      ')
      comment_body="${comment_body}

**Findings:**
${findings_text}"
    fi

    comment_body="${comment_body}

*Posted by theforge post_run hook.*"

    jq -n --arg body "$comment_body" --arg event "COMMENT" \
      '{body: $body, event: $event}' | \
      gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \
        --method POST \
        --input - || true
  done

  # Final APPROVE with merged summary
  approve_body="**theforge review pool APPROVED** — ${summary}

*Posted by theforge post_run hook.*"

  jq -n --arg body "$approve_body" --arg event "APPROVE" \
    '{body: $body, event: $event}' | \
    gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \
      --method POST \
      --input - || true
fi
