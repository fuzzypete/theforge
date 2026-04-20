# Story shape check module — standalone primitive for validating issue drafts

## What

A standalone shape-check module validates GitHub issue drafts and returns a
structured verdict.

## Why

TheForge increasingly generates issues as side-effects. The quality of
those drafts is uneven and needs a cheap mechanical pre-flight.

## Acceptance Criteria

- A standalone shape-check module accepts `(title, body, labels)` and
  returns a structured result. The module has no dependency on coordinator
  state, forge.yaml, or a provider/model. It must be importable from a
  GitHub Action runtime.
- The module returns structured data with a shape, a list of reasons, and
  a suggested action. Reasons include a stable code, a severity, and a
  human-readable detail.
- v1 deterministic checks cover the reason codes enumerated in the spec
  plus a seed vocabulary for cluster detection (operators extend the
  vocabulary via a shape-check parameter, not via forge.yaml).
- An optional classifier mode exists. The LLM mode is used for fuzzy
  checks and fails open into the heuristic result if unavailable. The
  checker runs with no provider credentials present.
- Classification outcomes match the spec: epics are tracking-only,
  superseded issues are closed, and untriaged forge findings need
  grooming.
- Tests cover the module as a pure function: fixtures per reason code,
  per shape outcome, and per classifier mode.
- A gold regression fixture feeds a verbatim policy-soup body into the
  checker and asserts the expected shape, code, and suggested action.
