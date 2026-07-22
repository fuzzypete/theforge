#!/usr/bin/env bash
# post_merge hook — fires after a story branch is successfully merged.
#
# Payload (stdin):
#   {
#     "event":     "post_merge",
#     "project":   "theforge",
#     "slug":      "my-feature",
#     "branch":    "feat/my-feature",
#     "merged_to": "main",
#     "run_id":    "abc123"
#   }
set -euo pipefail

payload=$(cat)
slug=$(echo "$payload" | jq -r '.slug')
branch=$(echo "$payload" | jq -r '.branch')
merged_to=$(echo "$payload" | jq -r '.merged_to')
echo "[forge post_merge] ${branch} → ${merged_to} (${slug})" >&2
exit 0
