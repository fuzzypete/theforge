---
name: "Progress-aware timeouts — detect stuck agents early"
slug: progress-aware-timeouts
pytest_target: tests/
---

# Progress-aware timeouts — detect stuck agents early

## Problem

Agents can get stuck in loops — making tool calls that don't make progress, re-reading the same files, or spinning on iterations without calling the expected submit tool. The current guard is a blunt max_iterations limit (e.g., 50), but reaching 50 iterations of no-progress burns significant budget before failing.

Real-world example: DeepSeek reviewer ran 50 iterations without ever calling `submit_review`, accumulating $0.45 and 1.66M input tokens on repeated file reads. An earlier intervention would have saved most of that cost.

## Solution

Track tool-call patterns during the agent loop and detect stuck behavior:

### Stuck indicators

1. **No submit after N iterations**: For review agents, if `submit_review` hasn't been called after 60% of max_iterations, inject a nudge prompt. If still not called at 80%, terminate early.
2. **Repeated tool calls**: If the same tool+arguments combination appears 3+ times in the last 10 iterations, the agent is likely looping.
3. **No file writes after N iterations**: For dev agents, if no Write/Edit tool calls appear after 50% of max_iterations, the agent may be stuck in analysis paralysis.

### Actions on stuck detection

- **Nudge** (soft): Inject a system message: "You appear to be stuck. Please proceed to submit your review / write your changes."
- **Terminate** (hard): After nudge fails to produce progress within 5 iterations, terminate with a clear error: "Agent terminated: no progress detected after {n} iterations (no submit_review call)"

### Implementation

- `ProgressTracker` class in `runner_api.py` (or shared `runner_common.py`)
- Tracks: tool calls per iteration, unique tool+args fingerprints, last submit/write call iteration
- Called at end of each iteration in the agent loop
- Returns: `ProgressStatus.OK | NUDGE | TERMINATE`
- The nudge prompt is already partially implemented (80% time-based nudge exists in runner_api.py). This extends it to be progress-aware, not just time-based.

### Configuration

Part of the profile config:

```yaml
profiles:
  review_pool:
    - name: deepseek-reviewer
      max_iterations: 50
      progress:
        nudge_at_pct: 60      # nudge at 60% of max_iterations with no submit
        terminate_at_pct: 80  # terminate at 80% with no submit
        loop_detection: true  # detect repeated tool calls
```

Defaults: nudge_at_pct=60, terminate_at_pct=80, loop_detection=true.

## Acceptance criteria

- ProgressTracker tracks tool call patterns across iterations
- Nudge prompt injected when no submit/write detected at nudge threshold
- Early termination when no progress at terminate threshold
- Repeated tool call detection (same tool+args 3+ times in 10 iterations)
- Configuration via profile config (nudge_at_pct, terminate_at_pct, loop_detection)
- Sensible defaults that would have caught the DeepSeek 50-iteration case
- Audit records stuck detection events
- Existing tests pass
- New tests for nudge, terminate, loop detection, configuration
