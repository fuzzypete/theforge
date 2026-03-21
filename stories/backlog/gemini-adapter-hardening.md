---
name: "Harden Gemini adapter against NoneType iteration errors"
slug: gemini-adapter-hardening
file_scope:
  - src/theforge/runner_api.py
  - tests/test_runner_api.py
pytest_target: tests/
---

# Gemini Adapter Hardening

## Problem

The Gemini loop adapter fails intermittently with:

```
Provider API error: 'NoneType' object is not iterable
```

Observed in production review pool runs against `gemini-2.5-flash` (google-genai
SDK v1.68.0). The error is caught by the generic `except Exception` in
`AgentLoopManager.run()` and reported as a pool failure. Because the verdict
is still APPROVE from the other reviewer, the sprint succeeds — but the gemini
reviewer produces no findings.

The error is intermittent: it appeared in a complex iOS/Swift review but not in
a simpler utility-function review using the same model and config.

## Root Cause Candidates

There are four places in `_make_google_adapter` where `None` could slip through
into an iteration context:

### 1. `candidate.content.parts` property access

```python
for part in (candidate.content.parts or []) if candidate.content else []:
```

In SDK v1.68.0, when a response is partially blocked (e.g., safety filter on
one candidate), `candidate.content` may exist as a non-None object but
`candidate.content.parts` may return `None` rather than `[]`. The guard
`if candidate.content else []` does not protect against this — it only guards
against `content` being None.

**Fix**: guard the parts attribute explicitly:
```python
parts = (candidate.content.parts if candidate.content else None) or []
for part in parts:
```

### 2. `dict(fc.args)` on MapComposite

```python
args = dict(fc.args) if fc.args else {}
```

In SDK v1.68.0, `function_call.args` changed from a protobuf `Struct` to a
`MapComposite`. When the model calls a tool with no arguments, `fc.args` may be
an empty MapComposite that is truthy (non-None object) but whose `dict()`
conversion iterates over `None` internally in some SDK builds.

**Fix**: use `dict(fc.args) if fc.args is not None else {}` and wrap in
try-except to fall back to `{}` on any conversion error:
```python
try:
    args = dict(fc.args) if fc.args is not None else {}
except (TypeError, AttributeError):
    args = {}
```

### 3. Empty tool results parts sent to Google

```python
elif role == "tool_results":
    parts = [{"function_response": ...} for r in msg.get("results", [])]
    result.append({"role": "user", "parts": parts})
```

If `results` is empty (shouldn't happen in practice but could if a tool returns
no output), Google receives `{"role": "user", "parts": []}` which is invalid
and may return an unexpected response object that causes downstream iteration
failures.

**Fix**: skip appending if parts is empty, or append a sentinel:
```python
if parts:
    result.append({"role": "user", "parts": parts})
```

### 4. Missing traceback context

The current error surface is only the exception message string. When the adapter
throws, the exact line and local state are lost. Add structured logging of the
exception traceback (at verbose level) so future failures are diagnosable
without a code change.

## Solution

1. **Fix `candidate.content.parts` guard** — separate the parts access from the
   content None check so both None-content and None-parts are handled.

2. **Fix `dict(fc.args)` conversion** — use `is not None` check and wrap in
   try-except with `{}` fallback.

3. **Fix empty `parts` in `tool_results`** — skip the append when parts is
   empty to avoid sending invalid content to the Google API.

4. **Add traceback logging** — in `AgentLoopManager.run()`, when the adapter
   raises, log the full traceback at verbose level before returning the failure
   result. Use `traceback.format_exc()`.

5. **Handle safety-blocked responses explicitly** — after iterating candidates,
   if `tool_calls` and `text_parts` are both empty, check
   `response.prompt_feedback` for a block reason and include it in the
   `LoopTurn.text_output` so the coordinator surfaces it.

## Acceptance Criteria

- [ ] `candidate.content.parts` is guarded against `None` independently of
      `candidate.content` being None
- [ ] `dict(fc.args)` conversion is wrapped in try-except; `{}` is used on
      failure
- [ ] Empty `tool_results` parts are not appended to the Google contents list
- [ ] `AgentLoopManager.run()` logs `traceback.format_exc()` at verbose level
      when the adapter raises
- [ ] Safety-blocked Gemini responses surface the block reason in `text_output`
      rather than silently returning empty
- [ ] New tests in `TestAgentLoopLifecycle` cover:
      - Adapter that raises `TypeError("'NoneType' object is not iterable")`
        → result is failure with `Provider API error` in output
      - Google adapter with `fc.args = None` → arguments parsed as `{}`
      - Google adapter with `candidate.content.parts = None` → graceful empty
        tool_calls and text
- [ ] All existing 827 tests pass
