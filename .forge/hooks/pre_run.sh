#!/usr/bin/env bash
# pre_run hook — fires before each forge run starts.
# Exit non-zero to abort the run (the only hook that can do so).
#
# Payload (stdin):
#   {
#     "event":   "pre_run",
#     "project": "theforge",
#     "slug":    "my-feature",
#     "story":   "specs/backlog/my-feature.md",
#     "spec":    "specs/backlog/my-feature.md",   # backward-compat alias
#     "run_id":  "abc123"
#   }
set -euo pipefail

payload=$(cat)
slug=$(echo "$payload" | jq -r '.slug')
echo "[forge pre_run] starting: ${slug}" >&2
exit 0
