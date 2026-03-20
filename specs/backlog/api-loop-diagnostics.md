---
name: "API agent loop diagnostics — per-turn tool call logging"
slug: api-loop-diagnostics
pytest_target: tests/
---

# API Agent Loop Diagnostics

## Problem

When an API agent (e.g. DeepSeek reviewer) churns through 50 iterations
without calling `submit_review`, we have no visibility into what it's
doing each turn. The structured log shows iteration count and final
outcome, but not:

- What tool calls the model requested each turn
- Whether `submit_review` was in the tool schema it received
- What the nudge prompt said and whether the model saw it
- The full conversation history that led to the churn

Without this, debugging issues like GH #11 (DeepSeek 50-iteration churn)
requires guesswork.

## Solution

Add diagnostic logging to the API agent loop in `runner_api.py`:

1. **Tool schema log at loop start** — log the names of all tools in
   the schema at verbose level so we can verify `submit_review` is
   present for the model.

2. **Per-turn tool call log** — log the tool call names (not arguments)
   each iteration at verbose level. Pattern:
   `[profile-name] iter N: K call(s): [tool1, tool2, ...]`

3. **Conversation dump on max-iterations** — when the loop hits
   `_max_iterations` without a submit, write the full `messages` list
   to a trace file at `.forge/traces/{cycle}-{profile}-conversation.json`.
   This is the key diagnostic artifact for post-mortem analysis.

4. **Nudge prompt logging** — when the 80% iteration nudge fires, log
   it at verbose level so we can see it was sent.

## Acceptance Criteria

- [ ] Loop start logs tool schema names at verbose level
- [ ] Each iteration logs tool call names at verbose level
- [ ] Max-iterations path writes conversation JSON to `.forge/traces/`
- [ ] Nudge prompt logged at verbose level when fired
- [ ] All existing tests pass
- [ ] No sensitive data (API keys, secrets) in diagnostic output
