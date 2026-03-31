---
name: "Dev prompt relaxes plan-obedience for note-driven and refactor stories"
slug: dev-prompt-plan-obedience
pytest_target: tests/test_dev_prompts.py
---

# Dev prompt relaxes plan-obedience for note-driven and refactor stories

## Problem

The dev prompt says "Follow it closely -- do not re-derive the approach from
scratch." This is appropriate for feature work where the plan captures
architectural decisions that the dev agent should not second-guess. But for
stories where the spec and Notes already contain the important constraints, or
for refactors where the implementation needs empirical discovery, strict
plan-obedience prevents the dev agent from adapting when it discovers the plan's
assumptions were wrong.

A dev agent that follows a wrong plan closely will produce wrong code, trigger
review rejection, and burn cycles patching within the plan's flawed frame rather
than stepping back and adjusting the approach. The plan should be a guide, not
a straitjacket -- but the current prompt language does not make that distinction.

## Expected behavior

The dev prompt's plan-obedience language varies based on story type or spec
sufficiency. Feature work retains the current "follow the plan closely"
instruction, since the plan reflects reviewed architectural decisions. Refactor
stories or note-driven stories (where the spec itself contains detailed
implementation guidance) get softer language that treats the plan as a starting
point: "Use the plan as a guide but adapt freely if you discover the approach
needs adjustment."

The story type or sufficiency classification that drives this distinction comes
from an upstream pipeline stage (preflight classification or story metadata).
The dev prompt builder selects the appropriate obedience framing based on that
signal.

## Acceptance criteria

- The dev prompt uses strict plan-obedience language ("follow the plan closely")
  for feature stories that went through full plan review
- The dev prompt uses relaxed plan-obedience language ("use the plan as a guide,
  adapt if needed") for refactor stories or stories classified as
  implementation-ready by preflight
- The obedience framing is driven by a signal from the pipeline (story type,
  preflight classification, or similar) rather than hardcoded heuristics in the
  prompt builder
- When no signal is available (backward compatibility), the dev prompt defaults
  to the current strict framing
- All existing tests pass
- New tests cover: prompt content for strict vs relaxed framing, default
  behavior when no classification signal is present

## Notes

- The current plan-obedience language is in the dev prompt builder, in the
  section that renders the structured plan as a step-by-step checklist. The
  exact text is: "Follow it closely -- do not re-derive the approach from
  scratch."
- This story has a natural dependency on `preflight-spec-sufficiency` for the
  classification signal, but could also be implemented with a simpler story-type
  heuristic (e.g., if the story name or slug contains "refactor") as a first
  pass.
- The relaxed framing should still encourage the dev agent to follow the plan's
  general direction -- the goal is "adapt when evidence contradicts the plan,"
  not "ignore the plan entirely."
