---
name: "Timeout-triggered model escalation"
slug: timeout-model-escalation
pytest_target: tests/
---

# Timeout-triggered model escalation

## Problem

When a dev agent times out (exit code -9 or equivalent), the coordinator currently treats it as a hard failure and escalates the entire sprint. But persistent-P1 model escalation already works: if the same P1 appears across 2+ review cycles, the coordinator bumps the dev model from sonnet to opus.

Timeout should trigger the same escalation logic. A timeout usually means the task was too complex for the current model — the same signal as a persistent P1, just expressed as a resource limit rather than a review finding.

## Solution

In the coordinator's dev-iteration loop, when a dev agent returns exit=-9 (or exceeds timeout_seconds):

1. Check if a larger model is available in the escalation chain (sonnet -> opus, etc.)
2. If yes: log the escalation, bump the model, increase timeout (use timeout_medium_seconds or timeout_large_seconds from profile config), and re-enter DEV
3. If no larger model available: proceed to ESCALATE as today

The escalation chain and timeout tiers already exist in config:
- `profiles.dev.timeout_seconds: 600`
- `profiles.dev.timeout_medium_seconds: 900`
- `profiles.dev.timeout_large_seconds: 1800`

### Guard rails

- Only escalate once per sprint (same as P1 escalation)
- Log clearly: "Dev agent timed out after 600s — escalating model from sonnet to opus with 900s timeout"
- Record in audit: `timeout_escalation: { from_model: sonnet, to_model: opus, original_timeout: 600, new_timeout: 900 }`

### CoordinatorState additions

- `timeout_escalated: bool = False`
- `timeout_escalation_detail: dict | None = None`

## Acceptance criteria

- Dev agent timeout (exit=-9) triggers model escalation when a larger model is available
- Escalation uses timeout_medium_seconds or timeout_large_seconds from profile
- Only one timeout escalation per sprint
- If no larger model available, proceeds to ESCALATE as before
- Audit YAML records timeout escalation details
- Existing tests pass
- New tests: timeout triggers escalation, timeout with no larger model proceeds to ESCALATE, double timeout doesn't double-escalate
