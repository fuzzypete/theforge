---
name: "Add reasoning_effort to ModelProfile for Codex"
slug: reasoning-effort
file_scope:
  - src/theforge/config.py
  - src/theforge/runner.py
  - tests/test_runner.py
pytest_target: tests/test_runner.py
---

# reasoning_effort in ModelProfile

## Problem

The Codex CLI supports `model_reasoning_effort` (`low`, `medium`, `high`) via
`-c model_reasoning_effort="high"`. TheForge passes `-m profile.model` to Codex
but has no way to set reasoning effort — it silently falls back to whatever is
in the user's `~/.codex/config.toml`. This means forge.yaml cannot control
the thinking depth for Codex review agents.

## Context

### What exists

- `ModelProfile` dataclass in `config.py`: `cli`, `model`, `budget_usd`,
  `timeout_seconds`, `allowed_tools`, `name`
- `_run_codex()` in `runner.py` builds: `npx @openai/codex exec --full-auto
  -m {model} -C {cwd} -o {output} {prompt}`
- Codex CLI accepts `-c key=value` for config overrides; `-c` can be
  repeated for multiple overrides

### What's missing

No `reasoning_effort` field on `ModelProfile`. No `-c model_reasoning_effort`
passed to Codex. Claude and Gemini runners are unaffected — this field is
Codex-only but should live on `ModelProfile` for future extensibility.

## Design

### `ModelProfile` change

Add optional field:

```python
@dataclass(frozen=True)
class ModelProfile:
    ...
    reasoning_effort: str | None = None  # "low" | "medium" | "high"; Codex only
```

### `_run_codex()` change

After `-m {model}`, append `-c model_reasoning_effort="{effort}"` if set:

```python
cmd: list[str] = [
    "npx", "@openai/codex", "exec", "--full-auto",
    "-m", profile.model,
]
if profile.reasoning_effort:
    cmd += ["-c", f'model_reasoning_effort="{profile.reasoning_effort}"']
cmd += ["-C", str(working_dir), "-o", str(output_file), prompt]
```

### Config parsing

In `_parse_profile()`:

```python
reasoning_effort = data.get("reasoning_effort")  # None if absent
```

Pass through to `ModelProfile(reasoning_effort=reasoning_effort, ...)`.

### Validation

In `_parse_profile()`, validate allowed values:

```python
VALID_REASONING_EFFORTS = {"low", "medium", "high"}
if reasoning_effort is not None and reasoning_effort not in VALID_REASONING_EFFORTS:
    raise ValueError(
        f"reasoning_effort must be one of {sorted(VALID_REASONING_EFFORTS)}, "
        f"got {reasoning_effort!r} in profile {name!r}"
    )
```

### forge.yaml example

```yaml
  review_pool:
    - name: codex
      cli: codex
      model: gpt-5.4
      reasoning_effort: high
      budget_usd: 1.00
      timeout_seconds: 600
```

## Acceptance Criteria

1. `ModelProfile` has `reasoning_effort: str | None = None`
2. When `reasoning_effort` is set, `_run_codex()` appends
   `-c model_reasoning_effort="{value}"` to the command
3. When `reasoning_effort` is None/absent, no `-c` flag is added
4. Config parser validates: only `low`, `medium`, `high` accepted
5. Invalid value raises `ValueError` with helpful message
6. Claude and Gemini runners are unchanged
7. Existing forge.yaml files without `reasoning_effort` continue to work

## Test Expectations

In `tests/test_runner.py`:

- `test_codex_reasoning_effort_high` — profile with `reasoning_effort="high"` →
  command includes `-c model_reasoning_effort="high"`
- `test_codex_reasoning_effort_none` — profile with `reasoning_effort=None` →
  command does NOT include `-c model_reasoning_effort`
- `test_codex_reasoning_effort_in_command_position` — `-c` flag appears after
  `-m model` and before `-C cwd` in the command list

In `tests/test_config.py` (or `test_coordinator.py`):

- `test_reasoning_effort_validation_invalid` — `reasoning_effort="extreme"` raises
  `ValueError`
- `test_reasoning_effort_validation_valid` — `low`, `medium`, `high` all parse
  without error
- `test_reasoning_effort_absent` — profile without field parses to `None`
