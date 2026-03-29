---
name: "Model preference lists for best-available provider fallback"
slug: model-preference-list
pytest_target: tests/
---

# Model Preference Lists

## Problem

Each forge.yaml profile today takes a single `model:` value. If that model is
unavailable (deprecated, quota exceeded, rate-limited past retry budget), the
reviewer fails and the story either stalls or escalates — even if an equivalent
model from the same provider is available.

Two concrete pain points:
1. Preview models (e.g. `gemini-3.1-pro-preview-customtools`) are renamed or
   deprecated without notice. A preference list lets forge fall through to the
   stable equivalent automatically.
2. Codex CLI occasionally exhausts usage quota mid-sprint. Today there is no
   fallback — the dev agent fails. A preference list that includes an API-backed
   model as the last entry allows the sprint to continue.

## Solution

Allow `model:` in a profile to be either a string (existing behavior, unchanged)
or an ordered list of model names. Forge tries each in order; the first successful
API call wins. "Successful" means the call returned a non-error response — not
just that the model name is valid.

For CLI-based profiles, the preference list may include a mix of CLI and API
entries. CLI entries are tried as subprocesses; API entries are tried via the
loop runner for the profile's provider. The first entry that completes without a
usage-exhaustion or model-not-found error wins.

The model actually used is recorded in the audit trail and emitted in the forge
log so the operator can see when a fallback fired.

```yaml
# Example: Gemini with stable fallback
- name: gemini-reviewer
  provider: google
  models:
    - gemini-3.1-pro-preview-customtools
    - gemini-2.5-pro
    - gemini-2.5-flash
  review_role: edge-cases
  budget_usd: 2.00
  timeout_seconds: 300
  max_iterations: 30

# Example: Codex with API fallback
- name: codex-dev
  cli: codex
  model: codex
  fallback_models:
    - gpt-4o        # API fallback when CLI quota exhausted
  budget_usd: 10.00
```

The scalar `model:` key remains valid and means a single-element list. The config
parser normalises both forms to `list[str]` internally.

For CLI profiles, the distinction between CLI and API entries in the list is
inferred from whether the entry matches a known CLI identifier (`claude`, `codex`,
`gemini`) or looks like an API model name. If ambiguous, treat as API.

## Acceptance criteria

- `model:` accepts a string (unchanged behavior) or an ordered list of strings
- Forge tries models in list order; first successful call wins
- When a fallback fires, the log emits which model was attempted, why it was
  skipped, and which model was ultimately used
- The audit trail records `model_used` (the actual model) alongside `model_config`
  (the configured preference list)
- For CLI profiles, API model names in the fallback list are invoked via the
  provider's API loop runner, not the CLI subprocess
- Usage-exhaustion and model-not-found errors are distinguished from other
  failures: only these trigger fallback; runtime errors (bad code, schema
  violations) do not
- A single-element list and a scalar string are equivalent — no behavior change
  for existing configs
- Tests cover: first model succeeds (no fallback), first fails with quota error
  (fallback fires), all models fail (error surfaces normally)
