---
name: "Complexity-proportional resource limits — auto-scale iterations and timeouts from complexity score"
slug: complexity-resource-scaling
pytest_target: tests/
depends_on: [domain-aware-routing]
---

# Complexity-Proportional Resource Limits

## Problem

Every resource limit in forge.yaml is authored by hand per agent, independent of
story complexity. The result is a proliferation of knobs with no principled basis:

```yaml
# today — manual, arbitrary, wrong for half of all stories
- name: codex-reviewer
  max_iterations: 15
  timeout_seconds: 300
- name: deepseek-reviewer
  max_iterations: 30
  timeout_seconds: 600
```

A complexity-1 refactor and a complexity-9 concurrent backend rewrite both get
the same limits. The codex reviewer timed out at 300s on a large story (observed).
DeepSeek's 30-iter limit is the right call for large work but wasteful on small
stories. There is no way to get this right with static config — the limits need to
track the work.

## Solution

Derive `max_iterations` and `timeout_seconds` at runtime from the preflight
complexity score (1-10, from domain-aware-routing). Remove the need to set these
per agent for the common case.

### Scaling model

For any phase (dev, reviewer, plan review):

```
effective_iterations = base_iterations × complexity_scale(score)
effective_timeout    = base_timeout    × complexity_scale(score)
```

Where `complexity_scale(score)` is a smooth function:

| Score | Multiplier |
|-------|-----------|
| 1-2   | 0.5       |
| 3-4   | 0.75      |
| 5-6   | 1.0       |
| 7-8   | 1.5       |
| 9-10  | 2.0       |

Interpolated linearly within each band. Base limits are set once, globally:

```yaml
workspace:
  base_dev_iterations: 10
  base_review_iterations: 20
  base_timeout_seconds: 600
  # complexity scaling is on by default
  complexity_scale: true
```

Per-agent `max_iterations` / `timeout_seconds` still accepted as hard ceilings
(not removed for backward compat), but no longer required. When absent, the
scaled value applies.

### What this replaces

- Manually tuning `max_iterations` per reviewer — gone for the common case
- Manually tuning `timeout_seconds` per phase — gone for the common case
- The codex/deepseek asymmetry — both get the same base, complexity does the rest
- Plan review max_iterations — scales from complexity of the plan being reviewed

### Interaction with progress-aware-timeouts

When both are active, `complexity_scale` sets the ceiling and
`progress-aware-timeouts` provides early exit within that ceiling. They
compose cleanly: a large story gets 40 iterations, stuck detection fires
at 60% (iter 24), terminates at 80% (iter 32). No manual tuning needed.

### Backward compatibility

- `complexity_scale: false` disables scaling entirely (existing behavior)
- Explicit per-agent limits act as ceilings (scale cannot exceed them)
- Missing complexity score (preflight failed) falls back to score=6 (1.0×)

## Acceptance criteria

- `complexity_scale: true` is the default when no explicit limits are set
- Effective iterations and timeout logged at run start so operators can see what was applied
- Scaling formula produces correct values for scores 1-10 (tested)
- Explicit per-agent limit acts as ceiling (scale never exceeds it)
- `complexity_scale: false` disables scaling and uses base limits as-is
- Preflight failure (score unknown) falls back to score=6 (no change from base)
- Plan review iterations scale from plan complexity score
- Dev iterations and timeout scale from story complexity score
- Reviewer iterations and timeout scale from story complexity score
- All existing tests pass
- New tests: scaling math, ceiling behavior, fallback, opt-out
