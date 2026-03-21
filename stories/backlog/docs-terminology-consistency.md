---
name: Audit and fix terminology consistency across all docs
slug: docs-terminology-consistency
pytest_target: tests/
---

# Audit and Fix Terminology Consistency Across All Docs

## Problem

Docs use inconsistent terminology: spec vs story, campaign vs sprint, brief vs
spec input, review pool vs reviewer pool, CLI mode vs API mode. For a tool built
around determinism, terminology drift undermines trust. New users wonder whether
docs are stale or a rename happened halfway through.

## Acceptance criteria

- Every doc file uses consistent terminology:
  - "story" (not "spec" when referring to the input document — except in file
    paths like `specs/` which are the convention)
  - "sprint" (not "campaign")
  - "brief" (for ideation input)
  - "review pool" (not "reviewer pool")
  - "CLI mode" and "API mode" used consistently
- The repo URL is consistent across all docs (github.com/fuzzypete/theforge)
- A terminology note in Getting Started or README clarifies the specs/ directory
  name vs "story" terminology ("stories live in `specs/` by convention")
- No broken cross-references between docs after the cleanup
