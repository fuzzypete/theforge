#!/usr/bin/env bash
# File GitHub issues for findings from ESCALATE or APPROVE-with-findings
set -euo pipefail
payload=$(cat)

verdict=$(echo "$payload" | jq -r '.verdict')
slug=$(echo "$payload" | jq -r '.slug')
branch=$(echo "$payload" | jq -r '.branch')
branch_display="${branch//\// -> }"
summary=$(echo "$payload" | jq -r '.summary')
findings_count=$(echo "$payload" | jq '.findings | length')

[ "$findings_count" -eq 0 ] && exit 0

echo "$payload" | jq -c '.findings[]' | while read -r finding; do
  sev=$(echo "$finding" | jq -r '.severity')
  file=$(echo "$finding" | jq -r '.file')
  line=$(echo "$finding" | jq -r '.line')
  desc=$(echo "$finding" | jq -r '.description')

  gh issue create \
    --title "[${sev}] ${slug}: ${desc}" \
    --body "**Story:** \`${slug}\` (branch \`${branch_display}\`)
**Verdict:** ${verdict} — ${summary}
**Location:** \`${file}\`${line:+ line ${line}}

${desc}

*Filed by theforge post_run hook.*" \
    --label "forge-finding" \
    --label "forge-${sev,,}" \
    --label "needs-triage" || true
done
