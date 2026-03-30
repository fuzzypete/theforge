---
name: "forge run/sprint: print config warnings at startup"
slug: startup-config-warnings
pytest_target: tests/
---

# forge run/sprint: print config warnings at startup

## Problem

Missing API keys, unreachable CLIs, and deprecated config fields are only
discovered when a story reaches the phase that needs the broken agent. A
sprint can burn $5 on planning before discovering the reviewer has no key.

## Expected behavior

- `forge run` and `forge sprint` call `check_agent_auth` on every profile
  after `load_config` succeeds, before entering the state machine
- Warnings (missing auth, deprecated fields) are printed to stderr
- Structural errors that would prevent any story from running (`load_config`
  raises `ValueError`) print the error and exit 2
- Warnings do not block — the run proceeds
- `--quiet` suppresses the warning block

## Acceptance criteria

- `forge run` prints auth warnings to stderr before WORKSPACE
- `forge sprint` prints auth warnings to stderr before first story
- Structural config errors exit 2 with a clear message
- Warnings do not block the run
- All existing tests pass
- New tests for warning output and exit behavior
