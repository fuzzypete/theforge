---
name: "spec-level gate override"
slug: gate-override
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/config.py
  - src/theforge/task.py
  - tests/test_coordinator.py
  - tests/test_config.py
pytest_target: tests/
---

# Spec-Level Gate Override

## Problem

The gate command is project-global (`forge.yaml` → `validation.gate_command`).
This works for code specs where `pytest` is the right gate, but fails for
non-code outputs:

- **Investigation specs**: output is a markdown diagnosis doc. `pytest` finds
  no new tests, or worse, fails on missing fixtures in the worktree.
- **Doc-only specs**: README updates, architecture docs, spec refinements.
- **Config-only specs**: forge.yaml tweaks, CI config changes.

These specs gate-fail repeatedly, burning dev iterations on a gate that can
never pass because it's testing the wrong thing. The review pool still
catches quality issues — the gate is just validating "does the code compile
and pass tests," which is meaningless for non-code output.

## Design

Spec frontmatter gains an optional `gate` key that overrides the project
gate for that spec only:

```yaml
---
name: "Polar BLE investigation"
slug: polar-ble-investigation
gate: none
---
```

The coordinator checks for `gate` in the parsed spec frontmatter before
falling through to `forge.yaml`'s global `validation.gate_command`.

### Gate modes

| Value | Behavior |
|-------|----------|
| _(absent)_ | Use project-global `validation.gate_command` (current behavior) |
| `none` | Skip gate entirely — VALIDATE always returns PASS |
| `lint` | Run only lint/format checks: `make fmt && make lint` (or project equivalent) |
| `"<custom command>"` | Run the specified command as the gate |

### How it works

1. `TaskSpec` gains `gate_override: str | None` parsed from frontmatter `gate` key
2. In VALIDATE phase, coordinator checks `task.gate_override`:
   - `None` → use `config.validation.gate_command` (unchanged)
   - `"none"` → skip gate, return PASS immediately
   - anything else → use that string as the gate command
3. Handoff file handling: when `gate_override` is `"none"`, no handoff file
   is expected. For custom commands, the same `handoff_file` / exit-code
   logic applies.

---

## Requirements

### R1: `gate` key in spec frontmatter

Optional string field. Parsed in `TaskSpec` (in `task.py`) alongside
existing frontmatter fields (`name`, `slug`, `file_scope`, `pytest_target`).

```python
@dataclass
class TaskSpec:
    ...
    gate_override: str | None = None  # from frontmatter "gate" key
```

### R2: Coordinator VALIDATE integration

In the VALIDATE phase of `_run()`, before running the gate command:

```python
if task.gate_override == "none":
    _log("  Gate override: none — skipping validation")
    # Record PASS, skip gate command entirely
    ...
elif task.gate_override is not None:
    gate_cmd = task.gate_override
    _log(f"  Gate override: {gate_cmd}")
else:
    gate_cmd = config.validation.gate_command
```

When `gate_override == "none"`:
- Do NOT run any subprocess
- Record gate decision as `"PASS"`
- Log: `[forge]   Gate override: none — skipping validation`
- Continue to REVIEW phase normally

When `gate_override` is a custom command string:
- Run that command instead of `config.validation.gate_command`
- Same timeout, working directory, and handoff parsing as the global gate
- If the custom command is simple (no `handoff.yaml` output), use exit-code
  mode (exit 0 = PASS, non-zero = FAIL)

### R3: Frontmatter validation

- `gate` must be a string if present
- No enum validation — any string is accepted (it's a shell command)
- The special value `"none"` (case-insensitive) triggers skip mode

### R4: CLI display

Show the gate override in the run header when active:

```
[forge]   Gate: none (spec override)
```

or:

```
[forge]   Gate: make lint (spec override)
```

### R5: Tests

- `test_gate_override_none_skips_validation`: spec with `gate: none` →
  VALIDATE phase returns PASS without running any subprocess
- `test_gate_override_custom_command`: spec with `gate: "make lint"` →
  runs `make lint` instead of global gate command
- `test_gate_override_absent_uses_global`: spec without `gate` key →
  uses `config.validation.gate_command` (existing behavior unchanged)
- `test_gate_override_parsed_from_frontmatter`: verify `TaskSpec.gate_override`
  populated from frontmatter
- `test_gate_override_none_case_insensitive`: `gate: None` and `gate: NONE`
  both trigger skip mode

---

## Acceptance Criteria

1. Spec frontmatter `gate: none` skips validation entirely
2. Spec frontmatter `gate: "make lint"` runs that command as the gate
3. Absent `gate` key uses the project-global gate (backward compatible)
4. Gate override logged clearly in run output
5. All existing tests pass
6. New tests cover skip, custom, and absent cases

## Out of Scope

- Per-spec `handoff_file` override (use exit-code mode for custom gates)
- Lint command auto-detection (user specifies the full command)
- Gate override in sprint manifests (spec-level only)
