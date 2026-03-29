---
name: "Harden Gemini adapter against silent empty responses"
slug: gemini-adapter-hardening
pytest_target: tests/
---

# Gemini Adapter Hardening

## Problem

The Gemini reviewer had a 100% failure rate across all stories in HDP sprints.
The immediate cause was a config misconfiguration: `provider: google` routed to
the single-shot API path (`_run_google`) instead of the Gemini CLI. This has been
fixed in HDP's forge.yaml (`cli: gemini`).

However, the API path itself has a fragility that would bite anyone using Gemini
via `provider: google`: if `response.text` is None (empty response from Gemini),
`json.loads(None)` raises `TypeError`. The broad `except Exception as e` catches
it and returns `AgentResult(exit_code=1, output="Google Gemini API error:
TypeError...")` — completely opaque.

The API path does not inspect:
- `prompt_feedback.block_reason` (safety filter triggered?)
- `candidates[].finish_reason` (SAFETY, RECITATION, MAX_TOKENS?)
- Input/output token counts (prompt too large?)

Any of these would explain an empty response, but the error surfaces as a generic
`TypeError` with no diagnostic information.

## Solution

Harden `_run_google` against empty or blocked responses:

1. After the API call, check `response.text` before calling `json.loads`. If None
   or empty, check `prompt_feedback.block_reason` and surface it explicitly.

2. Check `response.candidates` finish_reason for SAFETY, RECITATION, or other
   non-STOP reasons and include them in the error message.

3. Include input/output token counts in failure messages so the operator can
   diagnose whether the prompt size is the issue.

4. If the response is empty but not blocked (no block_reason, finish_reason=STOP),
   attempt one retry before giving up.

## Acceptance criteria

- `response.text is None` does not raise `TypeError` — it surfaces a clear error
  describing the empty response
- `prompt_feedback.block_reason` is checked and included in the error message when
  set
- Finish reasons other than STOP (SAFETY, RECITATION, MAX_TOKENS) are logged
  explicitly, not swallowed as generic exceptions
- Failure messages include input token count so operators can assess prompt size
- Existing successful behavior (response.text is valid JSON) is unchanged
- Tests cover: None response text, blocked response with block_reason, non-STOP
  finish reason, valid response (unchanged)
