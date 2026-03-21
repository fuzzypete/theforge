---
name: "DeepSeek loop diagnostics — investigate and fix 50-iteration churn"
slug: deepseek-loop-diagnostics
pytest_target: tests/
---

# DeepSeek Loop Diagnostics

## Problem

DeepSeek reviewer (`deepseek-chat`) runs for max iterations (now capped at 15,
previously 50) without ever calling `submit_review`. Each iteration sends the
full message history, burning budget on nothing. The agent appears to be making
tool calls (file reads, greps) every turn but never delivers a review verdict.

We don't know WHY because the loop doesn't log enough to diagnose:
- What tool calls the model makes each turn
- Whether `submit_review` is in the tool schema it receives
- What the model's text reasoning says between tool calls
- Whether the nudge prompt at 80% iterations has any effect

## Solution

Add diagnostic logging to the API agent loop in `runner_api.py` so that
the next time this happens, we can see exactly what went wrong.

### 1. Log tool schema at loop start (verbose)

When the agent loop starts, log the list of tool names available to the model.
This confirms `submit_review` (or `submit_plan_review`) is in the schema.

```
[forge]   [deepseek-reviewer] loop start: 5 tools: ['read_file', 'grep', 'glob', 'bash', 'submit_review']
```

### 2. Log per-turn tool calls (verbose)

After each iteration, log which tool calls the model requested:

```
[forge]   [deepseek-reviewer] iter 3: 2 call(s): ['read_file', 'grep']
```

### 3. Log nudge delivery

When the 80%-of-max-iterations nudge fires, log it at normal (not verbose)
level so we can see it in non-verbose runs:

```
[forge]   ⚠ deepseek-reviewer approaching iteration limit (12/15) — nudge sent
```

### 4. Log text reasoning snippets (verbose)

If the model returns text output alongside tool calls (reasoning), log the
first 200 chars at verbose level. This shows what the model is thinking:

```
[forge]   [deepseek-reviewer] iter 3 reasoning: "I need to check the test coverage for..."
```

### 5. Log iteration summary on failure

When max iterations is reached, log a summary of what happened across all
iterations — total tool calls by name, whether submit was ever attempted:

```
[forge]   ⚠ deepseek-reviewer max iterations (15): 45 tool calls [read_file:20, grep:15, glob:10], submit_review never called
```

## Acceptance Criteria

- [ ] Tool schema names logged at verbose level when agent loop starts
- [ ] Per-turn tool call names logged at verbose level after each iteration
- [ ] Nudge delivery logged at normal level (not just verbose)
- [ ] Text reasoning snippets (first 200 chars) logged at verbose level
- [ ] Iteration summary logged on max-iteration failure (tool call counts by
      name, whether submit tool was ever called)
- [ ] All logging uses existing `_log()` and `_log_verbose()` — no new
      logging infrastructure
- [ ] No functional changes to the agent loop — diagnostics only
- [ ] All existing tests pass
