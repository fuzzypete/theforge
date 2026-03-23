---
name: "Add --plan-model CLI override for runtime planner selection"
slug: plan-model-override
pytest_target: tests/
---

# --plan-model CLI Override

## Problem

`--dev-model` exists for overriding the dev agent model at runtime, but there's
no equivalent for the planner. Testing opus planning on a specific story requires
editing forge.yaml.

## Solution

Add `--plan-model` flag to `forge run`, mirroring `--dev-model` behavior.

Format: `provider/model` or `model` (defaults to cli=claude).

```bash
forge run story.md --plan-model opus
forge run story.md --plan-model anthropic/claude-opus-4-6
```

Implementation: in cli.py, add the argument and apply it via dataclasses.replace
on config.plan, same pattern as _apply_dev_model_override.

## Acceptance criteria

- `--plan-model opus` overrides the plan agent model
- `--plan-model provider/model` works like --dev-model format
- Original plan config preserved when flag not used
- Works with forge run (not needed for forge sprint)
- All existing tests pass
- New test for the override application
