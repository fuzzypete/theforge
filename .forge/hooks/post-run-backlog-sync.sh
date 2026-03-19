#!/usr/bin/env bash
# Update backlog.yaml stage based on forge outcome
set -euo pipefail
payload=$(cat)
outcome=$(echo "$payload" | jq -r '.outcome')
slug=$(echo "$payload" | jq -r '.slug')

case "$outcome" in
  done)     new_stage="review" ;;  # approved, awaiting merge
  escalate) new_stage="active" ;;  # needs human attention
  *)        exit 0 ;;
esac

# Project-specific: update backlog.yaml with yq or Python
# yq e "(.items[] | select(.slug == \"$slug\")).stage = \"$new_stage\"" -i backlog.yaml
echo "[$slug] stage → $new_stage"
