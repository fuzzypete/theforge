---
name: "Move transient artifacts under .forge/ directory"
slug: artifacts-under-forge
pytest_target: tests/
---

# Move Artifacts Under .forge/

## Problem

Dev agents write forge_plan.md, handoff.yaml, and forge_audit.yaml to the
worktree root. These cause merge conflicts on every branch merge and pollute
the project directory.

## Solution

Move all transient forge artifacts to .forge/ in the worktree:
- .forge/plan.md (was forge_plan.md)
- .forge/handoff.yaml (was handoff.yaml)
- .forge/audit.yaml (was forge_audit.yaml)

## Acceptance criteria

- Plan written to .forge/plan.md
- Handoff written to .forge/handoff.yaml
- Audit written to .forge/audit.yaml
- Gate reads handoff from new location
- Dev prompt tells agent to write to .forge/
- Backward compat: if old-location files exist, read from them
- Remove gitignore entries for root-level files
- All existing tests pass
