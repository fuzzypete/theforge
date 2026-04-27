# Runners subsystem guidance

## Purpose

The runners subsystem is the execution boundary between the deterministic
coordinator and external agent providers. It normalizes runner APIs, CLI-backed
invocations, provider adapters, tool runtime behavior, and finalization of agent
results.

## Invariants

- Runners execute work; they do not decide process policy. Keep routing,
  escalation, retry, and verdict logic in the coordinator.
- Preserve a clean separation between prompt input, provider invocation, and
  output normalization/parsing so provider-specific behavior does not leak across
  the subsystem.
- Provider adapters must maintain schema integrity. Do not silently coerce or
  discard malformed outputs in ways that hide contract violations.
- **Never invoke real provider CLIs** in the default gate (credentials, cost, non-determinism).
- **Runner lifecycle tests must use fake-CLI subprocess fixtures** (`tests/fake_bin/`),
  not subprocess mocks. Mocking `Popen`/`subprocess.run` is appropriate only for
  non-runner code paths; it cannot catch pipe-lifecycle bugs (stdin/stdout EOF,
  process exit timing, watchdog behaviour).
- Keep provider-specific code isolated to adapters or dedicated runner modules
  rather than spreading conditional logic throughout shared APIs.

## Context

- `api.py` defines the common runner-facing interface used by the coordinator.
- `cli.py` and `runner_*.py` modules wrap concrete agent execution paths.
- `adapters/` contains provider-specific translation layers for Anthropic,
  DeepSeek, Google, and OpenAI.
- `schema_utils.py` and `finalizers.py` are where output normalization and
  completion shaping happen; bugs here often surface later as validation or
  review failures.
- `tool_runtime.py` is the place to look when agent tool execution semantics or
  environment wiring are involved.
