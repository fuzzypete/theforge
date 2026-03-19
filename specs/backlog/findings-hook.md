---
name: "Findings hook — post-review callback with structured findings payload"
slug: findings-hook
pytest_target: tests/
---

# Findings Hook

## Problem

When a story ESCALATES or APPROVES with unresolved P2s, those findings vanish
into the audit log and are never tracked anywhere actionable. Teams want to
route findings to their issue tracker of choice — GitHub Issues, Jira, a local
file, a webhook — without baking any of those into the coordinator.

## Solution

A `findings_hook` in `forge.yaml`: an executable (script or binary) that the
coordinator invokes after every REVIEW phase that produces findings. The hook
receives a structured JSON payload on stdin describing the verdict, slug, and
findings. What the hook does with it is entirely up to the operator.

## forge.yaml config

```yaml
findings_hook:
  command: ".forge/hooks/findings.sh"
  on: [escalate, approve_with_findings]  # default: [escalate]
```

`on` controls when the hook fires:
- `escalate` — story reached ESCALATE (P1 persisted, max cycles hit)
- `approve_with_findings` — APPROVE but P2s remain
- `any_findings` — fire whenever there are any findings at all

## Payload (stdin, newline-terminated JSON)

```json
{
  "event": "escalate",
  "project": "hdp",
  "slug": "redesign-rest-timer-background",
  "spec": "specs/redesign-rest-timer-background.md",
  "branch": "feat/redesign-rest-timer-background",
  "run_id": "a3f9c12d",
  "verdict": "ESCALATE",
  "summary": "P1 persisted after 3 cycles: timer not resuming on unlock",
  "findings": [
    {
      "severity": "P1",
      "file": "gym-ui/WorkoutView.swift",
      "line": 142,
      "description": "Timer state not restored after WKExtendedRuntimeSession ends",
      "suggestion": "Store timer start time in UserDefaults before backgrounding"
    },
    {
      "severity": "P2",
      "file": "gym-ui/RestTimerView.swift",
      "line": 88,
      "description": "Progress ring resets to full on foreground",
      "suggestion": "Compute elapsed from stored start time rather than local state"
    }
  ],
  "cycles": 3,
  "total_cost_usd": 4.21
}
```

## Reference implementation: GitHub Issues

`.forge/hooks/findings-gh.sh` — committed to each project repo as opt-in:

```bash
#!/usr/bin/env bash
# findings-gh.sh — file GitHub issues for escalated findings
# Requires: gh CLI authenticated

set -euo pipefail
payload=$(cat)

slug=$(echo "$payload" | jq -r '.slug')
verdict=$(echo "$payload" | jq -r '.verdict')
summary=$(echo "$payload" | jq -r '.summary')
branch=$(echo "$payload" | jq -r '.branch')

echo "$payload" | jq -c '.findings[]' | while read -r finding; do
  severity=$(echo "$finding" | jq -r '.severity')
  file=$(echo "$finding"     | jq -r '.file')
  line=$(echo "$finding"     | jq -r '.line')
  desc=$(echo "$finding"     | jq -r '.description')
  suggestion=$(echo "$finding" | jq -r '.suggestion')

  label="forge-finding,${severity,,}"
  title="[${severity}] ${slug}: ${desc}"
  body="**Story:** \`${slug}\` (\`${branch}\`)
**Verdict:** ${verdict} — ${summary}
**Location:** \`${file}\` line ${line}

**Description:** ${desc}

**Suggestion:** ${suggestion}

*Filed automatically by theforge findings hook.*"

  gh issue create --title "$title" --body "$body" --label "$label" || true
done
```

## Implementation

### `src/theforge/config.py`
- New `FindingsHookConfig` dataclass: `command: str`, `on: list[str]`
- `ForgeConfig.findings_hook: FindingsHookConfig | None`
- Parse from `forge.yaml` `findings_hook` block

### `src/theforge/coord_phases.py`
- After review verdict is determined, call `_run_findings_hook()` if configured
- Check verdict + config `on` list to decide whether to fire
- Build payload dict from `CoordinatorState` + `ParsedReview`
- Serialize to JSON, invoke `command` via `subprocess.run` with stdin pipe
- Non-zero exit or exception: log warning, never fail the forge run
- Timeout: 30 seconds (hook should be fast; filing issues is cheap)

### `src/theforge/coord_util.py` (or new `coord_hooks.py`)
- `run_findings_hook(config, payload_dict)` — isolated, testable
- Returns `(success: bool, output: str)`

## Acceptance Criteria

- [ ] `forge.yaml` `findings_hook.command` is parsed into `ForgeConfig`
- [ ] Hook fires after ESCALATE when `on` includes `escalate` (default)
- [ ] Hook fires after APPROVE-with-findings when `on` includes `approve_with_findings`
- [ ] Payload JSON written to hook's stdin matches schema above
- [ ] Hook exit code non-zero: warning logged, forge run outcome unchanged
- [ ] Hook timeout (30s) exceeded: warning logged, forge run outcome unchanged
- [ ] `run_findings_hook()` is unit-tested with mocked subprocess
- [ ] Reference `findings-gh.sh` script committed under `.forge/hooks/`
- [ ] All existing tests pass
