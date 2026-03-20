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

payload=$(cat)

verdict=$(echo "$payload" | jq -r '.verdict')
slug=$(echo "$payload" | jq -r '.slug')
branch=$(echo "$payload" | jq -r '.branch')
summary=$(echo "$payload" | jq -r '.summary')
findings_count=$(echo "$payload" | jq '.findings | length')

# Only act on ESCALATE or APPROVE outcomes (REQUEST_CHANGES = still in review)
if [ "$verdict" != "ESCALATE" ] && [ "$verdict" != "APPROVE" ]; then
  exit 0
fi

# No findings → nothing to do
[ "$findings_count" -eq 0 ] && exit 0

echo "$payload" | jq -c '.findings[]' | while read -r finding; do
  sev=$(echo "$finding" | jq -r '.severity')
  file=$(echo "$finding" | jq -r '.file')
  line=$(echo "$finding" | jq -r '.line // empty')
  desc=$(echo "$finding" | jq -r '.description')
  suggestion=$(echo "$finding" | jq -r '.suggestion // empty')

  # Title: [P1] slug: description (truncated to 72 chars)
  raw_title="[${sev}] ${slug}: ${desc}"
  title="${raw_title:0:72}"

  location="\`${file}\`"
  [ -n "$line" ] && location="${location} line ${line}"

  body="**Story:** \`${slug}\` (\`${branch}\`)
**Verdict:** ${verdict} — ${summary}
**Location:** ${location}

**Description:** ${desc}"

  if [ -n "$suggestion" ]; then
    body="${body}

**Suggestion:** ${suggestion}"
  fi

  body="${body}

*Filed by theforge post_run hook.*"

  gh issue create \
    --title "$title" \
    --body "$body" \
    --label "forge-finding" \
    --label "$(echo "$sev" | tr 'A-Z' 'a-z')" || true
done

# ── PR Review Attribution ─────────────────────────────────────────────
# Opt-in: set FORGE_GH_PR_REVIEWS=1 to enable posting per-reviewer GitHub reviews
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
          "- [`\(.file):\(.line // "?")`] [\(.severity)] \(.description)"
        ) | join("\n")) + "\n\n"
      else "" end) +
      "*Posted by theforge post_run hook.*"
    end
  ')
  gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \
    --method POST \
    --field body="$reviewer_body" \
    --field event="APPROVE" || true
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
          "- [`\(.file):\(.line // "?")`] [\(.severity)] \(.description)"
        ) | join("\n")
      ')
      comment_body="${comment_body}

**Findings:**
${findings_text}"
    fi

    comment_body="${comment_body}

*Posted by theforge post_run hook.*"

    gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \
      --method POST \
      --field body="$comment_body" \
      --field event="COMMENT" || true
  done

  # Final APPROVE with merged summary
  approve_body="**theforge review pool APPROVED** — ${summary}

*Posted by theforge post_run hook.*"

  gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \
    --method POST \
    --field body="$approve_body" \
    --field event="APPROVE" || true
fi
