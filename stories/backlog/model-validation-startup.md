---
name: "Validate configured model names at startup"
slug: model-validation-startup
pytest_target: tests/
---

# Model Validation at Startup

## Problem

When a forge.yaml profile references a model name that doesn't exist or has been
deprecated, the run fails silently mid-cycle — often after spending budget on
earlier phases. There is no early warning that the configuration is broken.

This is compounded by fast-moving provider model namespaces (Google in particular
renames preview models frequently) and the fact that unknown models are not in
the pricing table, making cost estimation silently wrong as well.

## Solution

During the PREFLIGHT phase (or as a standalone `forge validate` command), forge
queries each configured provider's model list and checks that every model name in
forge.yaml resolves to a known, generatable model. Unreachable or unknown models
surface as a clear preflight failure before any budget is spent.

For providers that support a models-list API (Google, OpenAI, Anthropic), use the
live API. For CLI-based profiles (claude, codex), skip — validation is not
feasible without invoking the CLI.

Models not found in the provider's list produce a PREFLIGHT failure with a
message listing the invalid name and the closest available alternatives from the
same provider. Models found but absent from the internal pricing table produce a
warning (cost estimates will be $0 or null) but do not block the run.

The validation result is cached for the lifetime of the forge process — do not
re-query on every cycle.

## Acceptance criteria

- PREFLIGHT fails with a clear error if any API-backed profile's model name is
  not returned by the provider's model list
- The error message names the invalid model and lists available alternatives from
  the same provider
- Models found but missing from the pricing table produce a logged warning, not a
  failure
- CLI-based profiles (provider not in the API providers set) are skipped silently
- A `forge validate` subcommand runs model validation standalone without starting
  a full sprint
- Model list queries are cached per process — one query per provider per run
- Tests cover: invalid model name triggers preflight failure, missing pricing
  entry produces warning not failure, CLI profiles are skipped
