---
name: "Gate output tail in failure logs and dev feedback"
slug: gate-output-tail
file_scope:
  - src/theforge/coordinator.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Gate Output Tail

## Problem

When the gate fails, the coordinator logs only the first 200 characters of
output (`output[:200]`). Since gate output is ordered:

1. `make fmt` output (formatter echoing what it ran)
2. Linter output
3. TypeScript compiler output (if any)
4. pytest output (dots, then failures)

The actionable error — the actual failing test, the TypeScript error, the
specific lint violation — is almost always at the **end** of the output. It
lands at character 201+ and is silently discarded.

This caused multiple sprint tasks to escalate unnecessarily: the gate was
failing for a diagnosable reason (tsc error, pytest failure), the agent
couldn't see it, burned all 3 dev iterations making random changes, and
escalated. The worktrees were later confirmed to pass the gate after the sprint
ended.

## Root Cause

Two locations in `coordinator.py`:

```python
# Line 1152 — exit-code mode gate failure
_log(f"Gate command failed (exit non-zero): {output[:200]}")

# Line 1157 — handoff-based gate failure
_log_verbose(f"Gate command failed: {output[:200]}")
```

Both take the head of the output. Both should take the tail.

Additionally, when the coordinator sends the gate failure back to the dev agent
(via `human_feedback`), it includes the handoff file content but NOT the gate
command output. The agent has no visibility into why the gate failed.

## Solution

### 1. Change `output[:200]` → `output[-2000:]` in both locations

Show the last 2000 characters of gate output in logs. This captures the actual
error while keeping the log manageable.

### 2. Include gate output tail in dev agent feedback on FAIL

When the gate returns FAIL and the coordinator retries dev, include the gate
output tail in the human_feedback sent to the dev agent:

```python
state.human_feedback = (
    f"Gate returned {gate_decision}. Fix the issues and re-run the gate.\n\n"
    f"Gate output (last 2000 chars):\n{output[-2000:]}\n\n"
    f"Current handoff:\n{handoff_text}"
)
```

This gives the agent the actual error message — the tsc error, the failing
test, the lint violation — instead of forcing it to re-run the gate blind.

### 3. Add a `gate_output_tail_chars` config (optional, config.py)

Optional `ValidationConfig` field defaulting to 2000. Projects with noisy
gates can tune this up or down.

```yaml
validation:
  gate_output_tail_chars: 2000  # default
```

## Acceptance Criteria

- [ ] `output[:200]` replaced with `output[-{tail_chars}:]` in both gate
      failure log sites
- [ ] Gate output tail included in `human_feedback` when gate returns FAIL
      and dev is retried
- [ ] `gate_output_tail_chars` config field in `ValidationConfig` (default
      2000, backward compatible)
- [ ] `forge.yaml` parsing reads `gate_output_tail_chars`
- [ ] Existing tests pass without modification
- [ ] New test verifies gate failure log contains tail not head of output
- [ ] New test verifies dev feedback includes gate output tail on FAIL
