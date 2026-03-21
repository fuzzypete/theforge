---
name: "Lifecycle hooks — post_run, post_merge, post_sprint callbacks"
slug: lifecycle-hooks
pytest_target: tests/
---

# Lifecycle Hooks

## Problem

Forge outcomes (APPROVE, ESCALATE, findings, merges) are trapped in audit YAML
files. Teams want to act on these events — file GitHub issues, update a backlog,
sync Jira, post to Slack, append to a file — without any of those integrations
baked into the coordinator. The coordinator should be opinionated about *when*
to fire hooks, but agnostic about *what* they do.

## Solution

A `hooks` block in `forge.yaml` with lifecycle event hooks. Each hook is an
executable (script or binary) that receives a structured JSON payload on stdin.
Hooks are best-effort — they never fail the forge run.

## forge.yaml config

```yaml
hooks:
  post_run: ".forge/hooks/post-run.sh"
  post_merge: ".forge/hooks/post-merge.sh"
  post_sprint: ".forge/hooks/post-sprint.sh"
  pre_run: ".forge/hooks/pre-run.sh"  # optional
  timeout_seconds: 30  # per-hook, default 30
```

Each value is a command string. If the file doesn't exist or isn't executable,
the hook is silently skipped with a debug log.

## Hook events and when they fire

### `post_run` (highest value)

Fires after every `run_task` / `run_from_dev` / `run_from_review` completes —
after audit generation, before worktree cleanup. This is the single integration
point for:
- Filing GitHub/Jira issues for unresolved findings
- Updating backlog stage (active → review → done)
- Sending notifications beyond the built-in ntfy
- Triggering downstream CI

**Payload:**
```json
{
  "event": "post_run",
  "project": "hdp",
  "slug": "redesign-rest-timer-background",
  "spec": "specs/redesign-rest-timer-background.md",
  "branch": "feat/redesign-rest-timer-background",
  "run_id": "a3f9c12d",
  "outcome": "done",
  "verdict": "APPROVE",
  "summary": "Implementation meets spec; minor P2s noted",
  "cycles": 2,
  "dev_iterations": 3,
  "total_cost_usd": 5.40,
  "duration_seconds": 2172,
  "findings": [
    {
      "severity": "P2",
      "file": "src/theforge/coordinator.py",
      "line": 150,
      "description": "_TeeStderr.write() does not guard against write failures",
      "suggestion": "Wrap in try/except"
    }
  ],
  "gate_decisions": ["PASS", "PASS"],
  "review_pool": ["claude-reviewer", "codex-reviewer", "gemini-reviewer"],
  "review_pool_failed": ["deepseek-reviewer"]
}
```

For ESCALATE outcomes, `verdict` is `"ESCALATE"` and findings include the
persistent P1s that caused escalation.

### `post_merge`

Fires after auto-merge succeeds (branch merged to main). This is the
"stage → done + completed_at" moment.

**Payload:**
```json
{
  "event": "post_merge",
  "project": "hdp",
  "slug": "redesign-rest-timer-background",
  "branch": "feat/redesign-rest-timer-background",
  "merged_to": "main",
  "run_id": "a3f9c12d"
}
```

### `post_sprint`

Fires after `sprint-audit.yaml` is written. Batch update — multiple stories
may have changed state.

**Payload:**
```json
{
  "event": "post_sprint",
  "project": "hdp",
  "sprint": "observability-2",
  "run_id": "584eac5617f5",
  "total_cost_usd": 19.92,
  "duration_seconds": 7204,
  "stories": [
    {"slug": "run-log-capture", "outcome": "done", "verdict": "APPROVE", "merged": true},
    {"slug": "audit-improvements", "outcome": "done", "verdict": "APPROVE", "merged": true},
    {"slug": "provider-smoke-test", "outcome": "done", "verdict": "APPROVE", "merged": true}
  ]
}
```

### `pre_run` (optional)

Fires before WORKSPACE phase. Useful for auto-transitioning backlog stage
(ready → active). Receives minimal payload (slug, spec, run_id). Non-zero
exit **does** abort the run (only hook with this behavior) — useful as a
gate ("is this story in a runnable state?").

**Payload:**
```json
{
  "event": "pre_run",
  "project": "hdp",
  "slug": "redesign-rest-timer-background",
  "spec": "specs/redesign-rest-timer-background.md",
  "run_id": "a3f9c12d"
}
```

## Reference implementations

### `.forge/hooks/post-run-gh-issues.sh` — GitHub Issues for findings

```bash
#!/usr/bin/env bash
# File GitHub issues for findings from ESCALATE or APPROVE-with-findings
set -euo pipefail
payload=$(cat)

verdict=$(echo "$payload" | jq -r '.verdict')
slug=$(echo "$payload" | jq -r '.slug')
branch=$(echo "$payload" | jq -r '.branch')
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
    --body "**Story:** \`${slug}\` (\`${branch}\`)
**Verdict:** ${verdict} — ${summary}
**Location:** \`${file}\`${line:+ line ${line}}

${desc}

*Filed by theforge post_run hook.*" \
    --label "forge-${sev,,}" || true
done
```

### `.forge/hooks/post-run-backlog-sync.sh` — Backlog YAML updates

```bash
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
```

## Implementation

### `src/theforge/config.py`
- New `HooksConfig` dataclass: `post_run`, `post_merge`, `post_sprint`,
  `pre_run` (all `str | None`), `timeout_seconds: int = 30`
- `ForgeConfig.hooks: HooksConfig | None`
- Parse from `forge.yaml` `hooks` block

### New: `src/theforge/coord_hooks.py`
- `run_hook(command: str, payload: dict, timeout: int, label: str) -> HookResult`
  - Serialize payload to JSON, pipe to `subprocess.run` stdin
  - Returns `HookResult(success: bool, exit_code: int, output: str, duration_s: float)`
  - On exception or timeout: returns failure result, never raises
- `build_post_run_payload(state, config, task, audit) -> dict`
- `build_post_merge_payload(slug, branch, run_id, config) -> dict`
- `build_post_sprint_payload(sprint_name, stories, run_id, config) -> dict`
- `build_pre_run_payload(task, run_id, config) -> dict`

### `src/theforge/coordinator.py`
- After audit generation: call `post_run` hook if configured
- After merge: call `post_merge` hook if configured
- Before WORKSPACE: call `pre_run` hook if configured; non-zero aborts run
- Emit structured log event for each hook invocation:
  `{"event": "hook", "hook": "post_run", "success": true, "duration_s": 0.4}`

### `src/theforge/sprint.py`
- After `sprint-audit.yaml` is written: call `post_sprint` hook if configured

## Acceptance Criteria

- [ ] `forge.yaml` `hooks` block parsed into `ForgeConfig.hooks`
- [ ] `post_run` fires after every completed run with full payload
- [ ] `post_merge` fires after successful auto-merge
- [ ] `post_sprint` fires after sprint audit is written
- [ ] `pre_run` fires before WORKSPACE; non-zero exit aborts run
- [ ] All hook payloads include project, slug, run_id at minimum
- [ ] Hook timeout enforced (default 30s); exceeded = warning, not failure
- [ ] Hook exit non-zero (except pre_run): warning logged, run unaffected
- [ ] `run_hook()` unit-tested with mocked subprocess
- [ ] Reference scripts committed under `.forge/hooks/`
- [ ] Structured log event emitted for each hook invocation
- [ ] All existing tests pass
