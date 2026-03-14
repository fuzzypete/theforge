---
name: "Persistent structured logging"
slug: persistent-logging
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/sprint.py
  - src/theforge/config.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Persistent Structured Logging

## Problem

Forge's only durable output is `.forge/audits/` YAML files, written per-sprint
into the project repo. This means:

- No cross-sprint history without grepping YAML files
- No cross-project visibility
- Logs don't survive worktree cleanup
- No machine-readable stream for external analysis (what fails consistently,
  gate pass rates, cost trends, reviewer agreement rates over time)
- Debugging a bad run requires reconstructing context from memory

Every run disappears into a summary. There's no persistent record of what the
agent actually did, what the gate output was, or why something escalated.

## Solution

Append-only JSON Lines log at `~/.forge/logs/<project>/forge.log`. One JSON
object per line, one entry per significant event. The existing `.forge/audits/`
YAML summaries are unchanged — this adds a parallel machine-readable stream.

### Log location

```
~/.forge/logs/<project>/forge.log
```

`<project>` comes from `config.project` in `forge.yaml`. Directory is created
on first write. Log file is append-only — never truncated.

Optionally overridable in `forge.yaml`:
```yaml
logging:
  log_file: ~/.forge/logs/{project}/forge.log  # {project} is replaced
```

### Entry schema

Every entry is a single JSON object on one line with these common fields:

```json
{
  "ts": "2026-03-14T12:00:00Z",
  "project": "hdp",
  "run_id": "abc123",
  "task": "fix-gymapp-session-state",
  "event": "<event_type>",
  ...event-specific fields
}
```

`run_id` is a short random ID generated at the start of each `forge run` or
`forge sprint` invocation, stable across all events in that run.

### Event types

| event | when | key fields |
|---|---|---|
| `run_start` | forge run/sprint begins | `specs`, `budget_usd`, `resume` |
| `phase_start` | coordinator enters a phase | `phase`, `iteration` |
| `phase_end` | coordinator leaves a phase | `phase`, `outcome`, `cost_usd`, `duration_s` |
| `gate_result` | gate command completes | `decision`, `duration_s`, `output_tail` |
| `review_result` | synthesis verdict ready | `verdict`, `p1_count`, `p2_count`, `cost_usd` |
| `merge_result` | auto-merge attempt | `success`, `branch`, `error` |
| `run_end` | forge run/sprint ends | `outcome`, `total_cost_usd`, `total_duration_s` |
| `escalate` | task escalated | `reason`, `phase` |

`output_tail` on `gate_result` captures the last 500 chars of gate output —
enough to diagnose failures without storing full output.

### Implementation

- New `StructuredLogger` class in `coordinator.py` (or a new `logging.py`)
- Initialized once per run with `run_id`, `project`, `task`
- Called at each significant coordinator event
- Writes are best-effort: log failure never crashes the run (try/except around
  every write)
- `sprint.py` passes logger through to each task run

### LogConfig in config.py

```python
@dataclass(frozen=True)
class LogConfig:
    log_file: str = "~/.forge/logs/{project}/forge.log"
    enabled: bool = True
```

Parsed from optional `logging:` section in `forge.yaml`. Defaults work
without any config change (backward compatible).

## Acceptance Criteria

- [ ] `~/.forge/logs/<project>/forge.log` is created and appended on every run
- [ ] Each line is valid JSON with `ts`, `project`, `run_id`, `task`, `event`
- [ ] All event types from the table above are emitted at the right moments
- [ ] `gate_result` includes `output_tail` (last 500 chars of gate output)
- [ ] Log write failures are silently swallowed — never crash the run
- [ ] `LogConfig` parses from optional `logging:` section in `forge.yaml`
- [ ] `log_file` path supports `{project}` substitution and `~` expansion
- [ ] Existing tests pass without modification
- [ ] New tests verify events are emitted for a complete run lifecycle
- [ ] New tests verify log write failure is non-fatal
