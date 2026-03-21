---
name: "Run health metrics — cost proportions, anomaly detection, phase timing"
slug: run-health-metrics
pytest_target: tests/
---

# Run Health Metrics

## Problem

Runs complete with a binary success/fail, but there's no signal about
*how well* the run went. A successful run that burned $18 on 3 review
cycles looks the same as one that nailed it in a single pass for $6.
Operators can't tell if their config is working well or slowly degrading.

Real-world multi-agent systems show consistent cost and timing patterns
for healthy runs. Deviations from those patterns predict problems before
they become failures.

## Solution

Track per-phase cost and timing, compute health metrics, and surface
anomalies in the run summary and audit trail. No LLM involved — purely
mechanical computation from data the coordinator already collects.

## Health Metrics

### Cost Proportions (percentage of total run cost)

Reference ranges for a healthy MEDIUM-complexity run:

| Phase        | Healthy Range | Red Flag If       |
|-------------|---------------|-------------------|
| Preflight   | 1-5%          | > 10%             |
| Plan        | 5-15%         | < 3% (underinvest)|
| Plan Review | 3-8%          | > 15%             |
| Dev         | 40-60%        | > 75%             |
| Validate    | 1-5%          | > 10%             |
| Code Review | 20-35%        | > 45%             |

### Timing Proportions (percentage of total wall time)

| Phase        | Healthy Range | Red Flag If       |
|-------------|---------------|-------------------|
| Preflight   | 2-8%          | > 15%             |
| Plan        | 5-15%         | > 25%             |
| Plan Review | 5-12%         | > 20%             |
| Dev         | 35-55%        | > 70%             |
| Validate    | 2-8%          | > 15%             |
| Code Review | 15-35%        | > 50%             |

### Derived Signals

- **Review churn ratio**: `review_cycles / 1` — healthy is 1.0, warning at 2.0, red at 3.0
- **Plan regen ratio**: `plan_regen_count / 1` — healthy is 0, warning at 1, red at 2
- **Cost efficiency**: `total_cost / (complexity_multiplier * baseline_cost)` — 1.0 is nominal
- **First-pass rate**: across a sprint, what % of stories pass on first review cycle
- **Escalation rate**: across a sprint, what % of stories hit ESCALATE

### Anomaly Flags

Each metric gets a status: `nominal | warning | anomaly`. These are:
- Logged at verbose level always
- Included in the run summary block
- Included in audit YAML under `health_metrics:`
- Available to post_run hooks for external alerting

## Implementation

### Per-phase tracking (coordinator already has most of this)

```python
@dataclass
class PhaseMetrics:
    phase: str           # PREFLIGHT, PLAN, PLAN_REVIEW, DEV, VALIDATE, REVIEW
    wall_seconds: float
    cost_usd: float
    iterations: int      # dev iterations or review cycles
    model: str
    provider: str
```

### Health computation (new module: `src/theforge/health.py`)

```python
def compute_health(phases: list[PhaseMetrics], complexity: str) -> RunHealth:
    """Pure function. No LLM. Returns metrics + anomaly flags."""
```

### Output in run summary

```
── Run Health ──────────────────────────
  Phase        Cost     Time    Status
  PREFLIGHT    $0.12    12s     nominal
  PLAN         $0.85    45s     nominal
  PLAN_REVIEW  $0.40    38s     nominal
  DEV          $3.20   4m12s    nominal
  VALIDATE     $0.00     8s     nominal
  REVIEW       $1.80   2m15s    nominal
  ───────────────────────────────────
  Total        $6.37   7m50s
  Review churn: 1.0 (nominal)
  First-pass:   yes
```

### Output in audit YAML

```yaml
health_metrics:
  total_cost_usd: 6.37
  total_wall_seconds: 470
  review_churn_ratio: 1.0
  plan_regen_ratio: 0
  first_pass: true
  anomalies: []
  phases:
    - phase: PREFLIGHT
      cost_usd: 0.12
      wall_seconds: 12
      cost_pct: 1.9
      time_pct: 2.6
      status: nominal
    # ...
```

### Sprint-level aggregation

After all stories in a sprint complete, compute:
- Mean cost per story (by complexity)
- First-pass rate
- Escalation rate
- Cost distribution box-plot data (p25, p50, p75, p95)
- Worst offender stories (highest cost, most cycles)

This goes into the sprint summary and is available to post_sprint hooks.

## Reference Ranges Are Configurable

```yaml
health:
  review_cost_pct_warn: 40    # warn if review > 40% of cost
  review_cost_pct_red: 50     # anomaly if > 50%
  dev_cost_pct_warn: 70
  dev_cost_pct_red: 80
  # ... etc
```

Defaults match the table above. Most users won't touch these.

## Acceptance Criteria

- `PhaseMetrics` dataclass captures cost, time, iterations per phase
- `compute_health()` pure function computes proportions and anomaly flags
- Health metrics printed in run summary (verbose always, normal on anomaly)
- Health metrics written to audit YAML under `health_metrics:` key
- Reference ranges configurable via `health:` key in forge.yaml
- Sprint-level aggregation computes first-pass rate, escalation rate, cost stats
- Sprint summary includes aggregated health metrics
- No LLM calls in health computation — purely mechanical
- Tests cover nominal runs, warning triggers, and anomaly triggers
- Tests cover edge cases: zero-cost phases, skipped phases (e.g., no plan review)
