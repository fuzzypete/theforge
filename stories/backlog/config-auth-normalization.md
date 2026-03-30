---
name: "Normalize auth resolution into a single source of truth"
slug: config-auth-normalization
pytest_target: tests/
---

# Normalize auth resolution

## Problem

Auth resolution is scattered across 8+ files with inconsistent rules. Whether an
agent is "ready to run" depends on which code path asks: some check `os.environ`
only, some merge `config.secrets`, some skip auth for local endpoints, some don't.
CLI agents have different detection rules per runner (`shutil.which` vs `npx`).
Google has a `GEMINI_API_KEY` in the spec but runtime only checks `GOOGLE_API_KEY`.

This makes it impossible to build reliable config diagnostics (`forge check-config`)
because no single function can answer "will this agent work at runtime?"

## Solution

One function that answers "is this agent/profile ready to run?" using the same
rules the runtime actually uses. All existing callers migrate to it.

## Acceptance criteria

- A single auth-resolution function exists that all runtime callers use
- Auth checks merge `os.environ` with `config.secrets` (matching runtime behavior)
- CLI agents are checked using the actual invocation path (npx for codex/gemini, binary for claude)
- Local endpoints (localhost/127.0.0.1) skip API key requirements
- Google provider checks `GOOGLE_API_KEY` (and `GEMINI_API_KEY` as fallback) consistently everywhere
- `synthesis_profile` is included in auth validation alongside other profiles
- Unsupported provider/CLI values produce a clear error, not a KeyError
- All existing tests pass
- New tests for the unified auth function covering each auth path

## Notes

These are the known callers and inconsistencies discovered during plan review
of forge-check-config. Verify against current code before relying on them.

**Current auth check locations:**
- `src/theforge/assignment.py:31-42` — `_has_auth()` filters adaptive pool agents via `PROVIDER_API_KEY_MAP`
- `src/theforge/config/profiles.py:190-202` — warns on missing API key, skips for local endpoints
- `src/theforge/config/load.py` — `_validate_plan_provider()` raises ValueError on missing key
- `src/theforge/config/_loaders.py` — `_parse_plan_agent_review()` raises ValueError on missing key
- `src/theforge/runners/adapters/google.py:36-37` — constructs client with only `GOOGLE_API_KEY`
- `src/theforge/runners/adapters/openai.py:40-46` — allows local endpoint without key
- `src/theforge/runners/adapters/deepseek.py:23-27` — allows local endpoint without key
- `src/theforge/cli/providers.py:38-45` — treats `synthesis_profile` as a candidate

**CLI invocation patterns (from runners):**
- `runner_codex.py` — uses `npx @openai/codex`, not a `codex` binary
- `runner_gemini.py` — uses `npx @google/gemini-cli`, not a `gemini` binary
- `runner_claude.py` — uses `claude` binary directly

**Secret sources:**
- `os.environ` — direct env vars
- `config.secrets` — loaded from `.forge/.env` by `load_config()`
- `_resolve_secret()` in profiles.py checks secrets dict first, then env
- `tests/test_config_models.py:test_secret_satisfies_provider_api_key_validation` — asserts secrets-only auth works

**Known inconsistencies:**
- Google: spec says check `GOOGLE_API_KEY` or `GEMINI_API_KEY`, runtime only checks `GOOGLE_API_KEY`
- `synthesis_profile` is a real runtime agent but omitted from most validation paths
- `_validate_plan_provider()` hard-fails on missing auth instead of collecting warnings
- Deprecated `.forge/secrets.yaml` warning logic in `load.py:82-93` only fires when `.env` is absent
