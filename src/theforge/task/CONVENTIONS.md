# Task subsystem guidance

## Purpose

The task subsystem defines story data structures and builds the prompts and
parsers used for planning, development, fixes, and review. It is the prompt
construction layer, not the coordinator.

## Invariants

- Keep prompt construction separate from coordinator control flow and separate
  from runner/provider execution details.
<!-- forge-invariant id="acceptance-criteria-authoritative" scope="area:prompts area:story phase:plan,dev,review" enforcement="review" -->
- Preserve the distinction between story requirements and advisory notes.
  Acceptance criteria are authoritative; notes are hints that must not be
  promoted into hard requirements by default.
<!-- /forge-invariant -->
- Story and plan parsing should remain strict enough to surface malformed agent
  output instead of papering over ambiguity.
- Keep low-dependency data definitions lightweight; avoid pulling heavy runtime
  dependencies into foundational story or convention modules.
- Prompt changes that alter data exchanged between phases should be reflected in
  audit-visible structures upstream so the resulting values remain traceable.

## Context

- `story.py` defines the story/task representation consumed across the system.
- `dev_prompts.py`, `fix_prompts.py`, `plan_prompts.py`, and
  `review_prompts.py` hold phase-specific prompt builders.
- `plan_parser.py` is the main parsing boundary for structured planning output.
- `conventions.py` centralizes project conventions injected into prompts.
- If a change is about what agents are told or how their structured text is
  interpreted, this directory is usually the right home.
