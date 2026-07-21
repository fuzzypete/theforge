#!/usr/bin/env bash
# post_sprint hook — fires after a full sprint completes.
#
# Payload (stdin):
#   {
#     "event":            "post_sprint",
#     "project":          "theforge",
#     "sprint":           "sprint-name",
#     "run_id":           "abc123",
#     "total_cost_usd":   1.23,
#     "duration_seconds": 3600.0,
#     "stories": [
#       { "slug": "...", "outcome": "done|escalate", ... }
#     ]
#   }
set -euo pipefail

payload=$(cat)
sprint=$(echo "$payload" | jq -r '.sprint')
cost=$(echo "$payload" | jq -r '.total_cost_usd')
duration=$(echo "$payload" | jq -r '.duration_seconds')
story_count=$(echo "$payload" | jq '.stories | length')
echo "[forge post_sprint] ${sprint} complete — ${story_count} stories, \$${cost}, ${duration}s" >&2
exit 0
