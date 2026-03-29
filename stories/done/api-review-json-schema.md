---
name: "API reviewer JSON schema enforcement — drop YAML for API-mode reviewers"
slug: api-review-json-schema
pytest_target: tests/
---

# API Reviewer JSON Schema Enforcement

## Problem

All reviewers get YAML format instructions ("output ONLY a YAML block") regardless
of transport mode. API-mode reviewers (OpenAI, DeepSeek, Google) can enforce
output structure via `response_format` but we don't use it for the primary review
call — only for DeepSeek's finalizer retry.

This causes:
- DeepSeek consistently fails YAML formatting (missing sections, verdict
  contradictions) requiring 1-2 retries per review
- The repair layer (repair_review_yaml) papers over this but adds complexity
- Each retry burns tokens and adds 30-60s latency
- OpenAI's o4-mini spends 15-20 iterations exploring before calling submit_review

The hybrid-runner story (shipped) spec'd per-mode format instructions but it was
never fully implemented for all API providers.

## Solution

For API-mode reviewers, enforce the review schema at the provider level:

### OpenAI / DeepSeek
Use `response_format: { type: "json_object" }` (DeepSeek) or
`response_format: { type: "json_schema", json_schema: ... }` (OpenAI)
on the primary review call, not just the retry finalizer.

### Google
Use `response_schema` parameter with the review schema.

### Prompt changes
When building review prompts for API-mode profiles:
- Remove "output ONLY a YAML block" instruction
- Replace with "Return your review as a JSON object matching the provided schema"
- The schema is enforced by the provider, not the prompt

### CLI-mode
No change — Claude CLI reviewers continue to use YAML format instructions.

## Acceptance criteria

- API-mode reviewers use provider-level schema enforcement for review output
- Review prompt omits YAML instructions for API-mode profiles
- DeepSeek parse retry rate drops to near-zero
- OpenAI review iteration count drops (no format exploration)
- CLI-mode reviewers unchanged (YAML instructions preserved)
- repair_review_yaml still runs as safety net (defense in depth)
- All existing tests pass
