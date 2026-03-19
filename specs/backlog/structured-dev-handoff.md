---
name: "Structured dev handoff with schema validation"
slug: structured-dev-handoff
pytest_target: tests/
---

# Structured Dev Handoff

## Problem

The reviewer→dev handoff has a schema (`schemas.py`), cross-validation rules,
parse retries, and structured output. The dev→reviewer handoff has nothing.

Dev writes free-text `dev_notes` into `handoff.yaml` with no enforced structure.
The coordinator reads whatever the dev wrote and passes it straight to the
reviewer. If the notes are vague, missing, or malformatted, the reviewer gets
garbage context and flags correct decisions as violations.

When a human runs this workflow manually (copy/pasting between agents), they
succeed because they enforce clean handoff formatting before passing output to
the next agent. If the format is wrong, they ask the agent to rewrite it. The
coordinator doesn't do this — it accepts whatever comes out.

The review side already proves the pattern works: define a schema, validate it,
retry on failure. The dev side needs the same treatment.

## Requirements

1. Define a structured schema for the dev handoff — what was implemented,
   intentional spec deviations with justification, trade-offs made, what was
   deferred and why
2. The dev agent is prompted to produce the handoff in the specified format
   (already partially done via step 10 in `build_dev_prompt`)
3. After the dev agent completes and the gate passes, the coordinator validates
   the dev handoff against the schema
4. If validation fails, the coordinator sends the dev agent back with a
   focused prompt: "your handoff doesn't conform, rewrite it" — same pattern
   as review parse retries
5. The validated dev handoff is what the reviewer receives — not raw free text
6. A `validate_dev_handoff` function in `schemas.py` mirrors
   `validate_review_yaml`

## Acceptance Criteria

- [ ] `schemas.py` has a `validate_dev_handoff` function that checks the dev
      handoff structure
- [ ] Dev handoff schema requires: summary of what was implemented, list of
      spec deviations (or explicit "none"), list of deferred items (or
      explicit "none")
- [ ] `build_dev_prompt` instructs the dev to write handoff in the validated
      format
- [ ] Coordinator validates dev handoff after gate passes
- [ ] Invalid dev handoff triggers a retry with a focused "rewrite your
      handoff" prompt (not a full re-implementation)
- [ ] Retry is capped (e.g., 2 attempts) — if handoff still fails, proceed
      with whatever exists (don't block the pipeline)
- [ ] Valid dev handoff is passed to `build_review_prompt` as structured
      content, not raw text
- [ ] Reviewer prompt includes dev handoff sections (deviations, trade-offs)
      before the diff
- [ ] When dev handoff is absent or empty after retries, reviewer prompt is
      unchanged (no empty sections)
- [ ] All existing tests pass
