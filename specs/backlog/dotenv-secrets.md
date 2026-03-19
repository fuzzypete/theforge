---
name: "Migrate secrets to .forge/.env"
slug: dotenv-secrets
pytest_target: tests/
---

# Migrate Secrets to .forge/.env

## Problem

TheForge currently stores project-scoped secrets in `.forge/secrets.yaml`, a
bespoke YAML format. The ntfy notification URL is hardcoded in `forge.yaml`,
which is tracked by git — problematic for making the repository public.

The `.env` format is a universal convention. Every developer recognises it,
every secrets scanner flags it, and `python-dotenv` handles edge cases
(quoting, multiline, comments) without us writing a parser. Consolidating
API keys **and** user-specific config (ntfy URL) into a single `.forge/.env`
with a tracked `.forge/.env.example` gives a clean public-repo story.

## Goal

Replace `.forge/secrets.yaml` with `.forge/.env` (gitignored) and
`.forge/.env.example` (tracked), using `python-dotenv` for parsing.
Move the ntfy URL from `forge.yaml` into the env file so `forge.yaml`
contains no user-specific secrets or endpoints.

## Acceptance Criteria

### AC-1: New dependency
- Add `python-dotenv` to `pyproject.toml` dependencies
- Import and use `dotenv_values()` for loading `.forge/.env`

### AC-2: Loading
- `load_config()` reads `.forge/.env` via `dotenv_values()` if the file exists
- The loaded values are stored on `ForgeConfig.secrets: dict[str, str]`
  (same field as today — no downstream signature changes)
- `secrets` defaults to `{}` when the file is absent or empty
- Malformed `.env` raises a clear `ValueError` with the file path

### AC-3: Key resolution order (unchanged)
- `_resolve_secret()` checks `secrets` first, then `os.environ` — no change
- All existing callers (`_parse_profile`, plan_agent_review validation,
  runner subprocess injection) continue to work unmodified

### AC-4: Notification URL from env
- `forge.yaml` `notifications.ntfy.url` becomes optional
- If omitted, the coordinator reads `NTFY_URL` from `config.secrets`
  (i.e. from `.forge/.env` or the environment)
- If neither is set and ntfy backend is enabled, warn and disable notifications
  rather than crashing

### AC-5: .env.example (tracked)
- `.forge/.env.example` is committed to the repository
- Contents generated from `PROVIDER_API_KEY_MAP` plus `NTFY_URL`:

```
# .forge/.env — project-scoped secrets for TheForge
# Copy this file to .forge/.env and fill in the values you need.
# This file (.env.example) is tracked; .env is gitignored.

# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-proj-...
# GOOGLE_API_KEY=AIza...
# NTFY_URL=https://ntfy.sh/your-topic-here
```

### AC-6: CLI update
- `forge secrets-init` generates `.forge/.env` (not `secrets.yaml`)
- Skeleton content matches `.env.example`
- Adds `.forge/.env` to `.gitignore` (replaces the `secrets.yaml` entry)
- If `.forge/.env` already exists, prints warning and exits without overwriting
- `forge init` also runs the gitignore logic

### AC-7: Migration
- If `.forge/secrets.yaml` exists and `.forge/.env` does not, print a
  one-time warning: `⚠ .forge/secrets.yaml detected — migrate to .forge/.env
  (see .forge/.env.example)`
- Do NOT auto-migrate — the user may have secrets they want to review
- Remove `secrets.yaml` loading code after one release cycle (or immediately
  if we haven't shipped a public release yet)

### AC-8: .gitignore update
- Replace `.forge/secrets.yaml` entry with `.forge/.env`
- Keep `.forge/` directory ignore (covers worktrees, audits, etc.)

### AC-9: Tests
- `test_config.py`: `.env` file loaded and merged into `ForgeConfig.secrets`
- `test_config.py`: missing file → empty dict
- `test_config.py`: provider key in `.env` satisfies API key validation
- `test_config.py`: `NTFY_URL` from env used when `forge.yaml` omits the URL
- `test_runner.py`: secrets injection unchanged (dict interface is the same)
- `test_cli.py`: `forge secrets-init` creates `.env` skeleton
- `test_cli.py`: migration warning emitted when `secrets.yaml` exists

## Implementation Notes

### Config loading change
```python
from dotenv import dotenv_values

env_path = project_root / ".forge" / ".env"
secrets: dict[str, str] = {}
if env_path.exists():
    raw = dotenv_values(env_path)
    secrets = {k: v for k, v in raw.items() if v is not None}
```

### Notification config resolution
```python
ntfy_url = ntfy_config.get("url") or secrets.get("NTFY_URL") or os.getenv("NTFY_URL")
if not ntfy_url:
    log.warning("ntfy backend enabled but no URL configured — notifications disabled")
```

### forge.yaml change
```yaml
notifications:
  backend: ntfy
  ntfy:
    # url now comes from .forge/.env NTFY_URL
    priority: high
```

## Out of Scope

- Encryption at rest
- Per-profile key isolation
- Remote secret stores
- Backward-compatible `secrets.yaml` loading beyond the migration warning
