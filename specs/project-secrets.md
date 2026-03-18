---
name: "Project-scoped secrets"
slug: project-secrets
pytest_target: tests/
---

# Project-Scoped Secrets

## Problem

TheForge validates API keys by reading `os.environ` — the caller's global shell
environment. This means users must export `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
etc. into `~/.zshrc` or `~/.bash_profile`, where they leak to every process in
the shell session, not just forge invocations.

As the hybrid runner adds real API calls (providers: openai, anthropic, google),
there is no ergonomic, project-local place to drop a new key without touching
global shell config.

## Goal

A gitignored `.forge/secrets.yaml` in the project root that:
1. Contains only the keys needed for *this project's* forge config
2. Is injected into agent subprocess environments — never into the parent shell
3. Is generated as a commented-out skeleton on `forge init` (or `forge secrets init`)
   so operators can discover and fill in only the keys they need
4. Stays in sync with `PROVIDER_API_KEY_MAP` — adding a new provider automatically
   adds its key to the skeleton template

## Acceptance Criteria

### AC-1: Loading
- `load_config()` (in `config.py`) reads `.forge/secrets.yaml` from the project
  root if the file exists
- The loaded values are stored on `ForgeConfig` as `secrets: dict[str, str]`
- `secrets` defaults to `{}` when the file is absent or empty
- Malformed YAML in secrets file raises a clear `ValueError` with the file path

### AC-2: Key resolution order
- When validating provider API keys (in `_parse_profile` and plan_agent_review
  validation), check `secrets` first, then fall back to `os.environ`
- A key present in `secrets` satisfies the validation even if absent from env

### AC-3: Subprocess injection
- `runner.py` `run_agent()` and `run_api_agent()` accept `secrets: dict[str, str]`
  and merge them into the subprocess/SDK client env: `{**os.environ, **secrets}`
  — secrets win over env, env provides defaults (e.g. PATH, HOME)
- The coordinator passes `config.secrets` to every `run_agent` / `run_api_agent` call
- The parent process `os.environ` is never mutated

### AC-4: Skeleton generation
- `forge secrets init` (new subcommand) writes `.forge/secrets.yaml` if it does
  not already exist
- The skeleton lists every key in `PROVIDER_API_KEY_MAP` as a commented-out entry:

```yaml
# .forge/secrets.yaml
# Project-scoped API keys for TheForge.
# This file is gitignored. Do not commit it.
#
# Uncomment and fill in the keys needed for your forge.yaml profiles.

# ANTHROPIC_API_KEY: sk-ant-...
# OPENAI_API_KEY: sk-proj-...
# GOOGLE_API_KEY: AIza...
```

- If the file already exists, `forge secrets init` prints a warning and exits
  without overwriting
- `forge secrets init` also adds `.forge/secrets.yaml` to `.gitignore` (appends
  the line if not already present); if `.gitignore` does not exist it is created

### AC-5: gitignore enforcement
- `forge init` (existing command) also calls the gitignore logic from AC-4 so
  new projects are protected from the first run
- If `.forge/secrets.yaml` is tracked by git (i.e. not in `.gitignore`), forge
  prints a one-time warning at startup: `⚠ .forge/secrets.yaml is not gitignored`

### AC-6: Tests
- `test_config.py`: secrets file loaded and merged into `ForgeConfig.secrets`
- `test_config.py`: missing file → empty dict (no error)
- `test_config.py`: malformed YAML → `ValueError` with file path in message
- `test_config.py`: provider key in secrets satisfies API key validation even
  when `OPENAI_API_KEY` not in `os.environ`
- `test_runner.py`: `run_agent()` merges secrets into subprocess env correctly
- `test_cli.py`: `forge secrets init` creates skeleton file and updates `.gitignore`
- `test_cli.py`: `forge secrets init` is a no-op (with warning) when file exists

## Implementation Notes

### ForgeConfig change
```python
@dataclass
class ForgeConfig:
    ...
    secrets: dict[str, str] = field(default_factory=dict)
```

### load_config() addition
```python
secrets_path = project_root / ".forge" / "secrets.yaml"
secrets: dict[str, str] = {}
if secrets_path.exists():
    try:
        raw = yaml.safe_load(secrets_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{secrets_path}: secrets file must be a YAML mapping")
        secrets = {str(k): str(v) for k, v in raw.items()}
    except yaml.YAMLError as e:
        raise ValueError(f"{secrets_path}: malformed YAML — {e}") from e
```

### Key resolution helper
```python
def _resolve_secret(key: str, secrets: dict[str, str]) -> str | None:
    return secrets.get(key) or os.getenv(key)
```

### Skeleton template
The skeleton is generated from `PROVIDER_API_KEY_MAP` programmatically, not
hardcoded, so adding a new provider to the map automatically appears in the
template.

### Subcommand
`forge secrets init` is a new Click subcommand group under the existing `forge`
CLI. Keep it simple — no subcommand group needed, just `forge secrets-init` is
fine if it avoids Click complexity.

## Out of Scope

- Encryption at rest (use OS keychain or 1Password CLI for that)
- Per-profile key isolation (one key per provider, shared across all profiles)
- Remote secret stores (AWS Secrets Manager, Vault, etc.)
- Key rotation or expiry
