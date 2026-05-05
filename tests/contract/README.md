# CLI contract tests

This layer validates that the argv each runner constructs is actually accepted
by the installed provider CLI. It complements (does not replace) the mocked
unit tests in `tests/test_runner_*.py`.

## Running

Default test gate skips this layer. Opt in with:

```
THEFORGE_RUN_CLI_CONTRACT=1 .venv/bin/python -m pytest tests/contract -v
```

Tests auto-skip individually if the corresponding CLI is not on `PATH`, so a
contributor with only `claude` installed will run the Claude contract tests
and skip the others.

## Scope

Covered: every runner that spawns a provider CLI subprocess
(`runner_claude.py`, `runner_codex.py`, `runner_gemini.py`).

Excluded: `adapters/deepseek.py`. DeepSeek is an HTTP-API adapter — it has no
subprocess argv surface to validate. If a CLI-backed DeepSeek runner is added
later, contract coverage must accompany it (enforced by the
`test_runner_contract_coverage` check in `tests/test_conventions.py`).

## Adding a new runner

1. Add a `build_argv` (and any resume/variant) function to the runner module.
2. Create `tests/contract/test_<name>_cli_contract.py` with
   `pytestmark = pytest.mark.cli_contract`, calling `require_cli(...)` and
   `assert_cli_accepts_argv(...)` for every argv shape the runner emits.
3. The conventions check enforces step 2 — adding a runner without a contract
   file will fail the default gate.
