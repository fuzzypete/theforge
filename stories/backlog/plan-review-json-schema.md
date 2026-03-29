---
name: "Plan review JSON schema enforcement — extend API mode fix to plan review path"
slug: plan-review-json-schema
pytest_target: tests/
---

# Plan Review JSON Schema Enforcement

## Problem

`api-review-json-schema` (shipped) enforced `response_format: json_object`
for API-mode reviewers in the **code review** path. The plan review path was
not updated.

DeepSeek in plan review still produces YAML with verdict contradictions —
`verdict: APPROVE` alongside P1 findings — which trips the schema validator:

```
verdict is APPROVE but 1 P0/P1 finding(s) exist — cannot approve with
blocking findings
```

This excludes DeepSeek from every plan review cycle, leaving Codex as the
sole plan reviewer. Observed in every refactor sprint run.

## Solution

Apply the same fix to the plan review submission path:

- For DeepSeek (`provider: deepseek`) plan reviewers, use
  `response_format: {"type": "json_object"}` on the plan review call
- For OpenAI plan reviewers, use `response_format: {"type": "json_schema", ...}`
  with the plan review schema
- Plan review prompt for API-mode reviewers: replace YAML output instructions
  with `submit_plan_review` tool instructions (same pattern as code review)

## Acceptance criteria

- DeepSeek plan reviewer uses `response_format: json_object` — no more
  YAML verdict contradictions
- Plan review prompt for API-mode profiles omits YAML instructions, uses
  `submit_plan_review` tool
- CLI-mode plan reviewers unchanged
- DeepSeek plan review parse error rate drops to near-zero
- All existing tests pass
- New tests mirror the api-review-json-schema tests for the plan review path
