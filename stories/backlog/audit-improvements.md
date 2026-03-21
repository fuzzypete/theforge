---
name: Audit log and display improvements
slug: audit-improvements
pytest_target: tests/
---

# Audit Log and Display Improvements

## Problem

The `forge audit` command output is minimal — it shows a flat summary but
lacks the detail needed to understand what actually happened during a run.
The audit YAML itself is also missing timing data, which is critical for
understanding where time (and money) was spent.

## Requirements

### 1. Add timing data to audit log

The `generate_audit_log()` output should include:

- `started_at` and `finished_at` ISO timestamps for the overall run
- `duration_seconds` for the overall run
- Per-agent `duration_seconds` in the cost section (dev vs review time)

This requires `CoordinatorState` to track `started_at` (set in INIT phase)
and each `AgentResult` to already carry duration (it does — the runner
records elapsed time).

### 2. Add per-agent cost breakdown

The cost section should break out individual agent invocations:

```yaml
cost:
  total_usd: 0.450
  dev_usd: 0.300
  review_usd: 0.150
  dev_invocations: 2
  review_invocations: 3
  agents:
    - role: dev
      profile: sonnet
      cost_usd: 0.150
      duration_seconds: 45
    - role: dev
      profile: sonnet
      cost_usd: 0.150
      duration_seconds: 52
    - role: review
      profile: critic-a
      cost_usd: 0.050
      duration_seconds: 30
```

### 3. Improve `forge audit` display

The `cmd_audit` function should display:

- Duration and timestamps
- Per-agent cost breakdown (table format)
- Review findings detail: show each P1/P2 finding's file, line, and
  one-line description (not just counts)
- Workspace path and branch

### 4. Add review findings to audit YAML

Each review cycle entry should include the actual findings list, not just
counts:

```yaml
reviews:
  - cycle: 1
    verdict: REQUEST_CHANGES
    summary: "..."
    findings:
      - severity: P1
        file: src/foo.py
        line: 42
        description: "Bug in error handling"
      - severity: P2
        file: src/bar.py
        line: 10
        description: "Missing docstring"
```

## Acceptance Criteria

- [ ] `CoordinatorState` has a `started_at: str | None` field set during INIT
- [ ] `generate_audit_log()` includes `started_at`, `finished_at`, `duration_seconds`
- [ ] Audit YAML `cost.agents` list has per-invocation entries with role, profile, cost, duration
- [ ] Audit YAML `reviews[].findings` includes the actual findings (not just counts; counts stay too)
- [ ] `cmd_audit` displays timing, per-agent costs, and finding details
- [ ] All existing tests continue to pass
- [ ] New tests cover the added audit fields
