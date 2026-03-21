---
name: "Phase telemetry — per-phase cost, duration, and success tracking"
slug: phase-telemetry
pytest_target: tests/
---

# Phase Telemetry

## Problem

The structured log has per-phase events (phase_start, phase_end, cost, duration)
but there's no aggregation. To answer "where does my budget go?" you have to
grep JSONL and do arithmetic manually. There's no way to compare cost efficiency
across sprints, identify which phases are expensive, or track improvement over
time.

## Solution

Add a telemetry summary that aggregates per-phase metrics from the structured
log. Two outputs:

1. **Per-run summary** in the audit log (already partially there — extend it)
2. **Cross-run dashboard** via \`forge telemetry\` CLI command

### Per-run telemetry (audit log extension)

Add a \`phases\` block to the audit YAML:

\`\`\`yaml
phases:
  preflight:
    cost_usd: 0.11
    duration_s: 31
    outcome: proceed
  plan:
    cost_usd: 0.21
    duration_s: 78
    outcome: success
  plan_review:
    cost_usd: 0.53
    duration_s: 73
    iterations: 1
    outcome: approve
  dev:
    cost_usd: 3.26
    duration_s: 969
    iterations: 1
    outcome: success
  validate:
    cost_usd: 0.00
    duration_s: 12
    outcome: pass
  review:
    cost_usd: 1.77
    duration_s: 208
    cycles: 1
    outcome: approve
    per_reviewer:
      claude-reviewer: { cost: 0.50, verdict: APPROVE }
      codex-reviewer: { cost: 0.45, verdict: APPROVE }
      gemini-reviewer: { cost: 0.01, verdict: APPROVE }
      deepseek-reviewer: { cost: 0.45, verdict: APPROVE }
totals:
  cost_usd: 5.88
  duration_s: 1371
  dev_iterations: 1
  review_cycles: 1
\`\`\`

### CLI: forge telemetry

\`\`\`bash
forge telemetry                    # last 10 runs
forge telemetry --since 2026-03-01 # date filter
forge telemetry --phase review     # filter to one phase
\`\`\`

Output:

\`\`\`
Run                    Cost    Time   Dev Iters  Review Cycles  Outcome
review-convergence    $7.24   28m    1          1              done
stale-approve-triage  $2.94   13m    1          2              done
drop-file-scope       $7.54   23m    2          1              done
pr-review-attribution $9.09   43m    2          3              escalate

Phase breakdown (last 10 runs):
  PREFLIGHT   avg $0.12   avg  35s
  PLAN        avg $0.22   avg  85s
  PLAN_REVIEW avg $0.55   avg  90s
  DEV         avg $2.84   avg 620s   ← 48% of cost
  VALIDATE    avg $0.00   avg  11s
  REVIEW      avg $1.20   avg 180s   ← 20% of cost
\`\`\`

### Data source

Read from \`.forge/audits/history.jsonl\` (already has per-run records) and
\`.forge/logs/forge.log\` (has per-phase events with cost and duration). Cross-
reference by run_id.

## Acceptance Criteria

- [ ] Audit log gains \`phases\` block with per-phase cost, duration, outcome
- [ ] Per-reviewer cost breakdown in review phase
- [ ] \`forge telemetry\` CLI command reads history and displays summary table
- [ ] Phase breakdown shows average cost and duration across runs
- [ ] \`--since\` date filter
- [ ] \`--phase\` filter to show single phase detail
- [ ] Handles missing/partial data gracefully (old runs without phase data)
- [ ] All existing tests pass
- [ ] New tests for telemetry aggregation and CLI output formatting
