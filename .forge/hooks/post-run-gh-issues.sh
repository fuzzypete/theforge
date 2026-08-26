#!/usr/bin/env bash
# File GitHub issues for findings from ESCALATE or APPROVE-with-findings
set -euo pipefail
payload=$(cat)

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
    --title "$1" \
    --body-stdin \
    --label "bug" \
    --label "forge-finding" \
    --label "needs-triage" \
    --label "$2"
}

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

  title="[${sev}] ${slug}: ${desc}"
  sev_label=$(echo "$sev" | tr 'A-Z' 'a-z')
  body="**Story:** \`${slug}\` (branch \`${branch_display}\`)
**Verdict:** ${verdict} — ${summary}
**Location:** \`${file}\`${line:+ line ${line}}

${desc}

*Filed by theforge post_run hook.*"

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
