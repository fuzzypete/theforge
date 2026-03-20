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
    --label "${sev,,}" || true
done
