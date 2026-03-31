---
name: "Coordinator removes handoff.yaml from git index on workspace setup"
slug: forge-handoff-git-hygiene
pytest_target: tests/
---

# Coordinator removes handoff.yaml from git index on workspace setup

## Observed behavior

`.forge/handoff.yaml` periodically reappears as a tracked git file, causing
merge conflicts and dirty worktrees. It is gitignored but still causes conflicts
when it gets into the index — either from old commits before gitignore was added,
or from agent misbehavior.

## Expected behavior

During workspace setup, the coordinator deterministically checks whether
`.forge/handoff.yaml` (and `.forge/trajectory.yaml`) are tracked in the git
index and runs `git rm --cached` on them if so. This is idempotent and safe —
if the file isn't tracked, the command is a no-op.

The fix should live in the workspace setup phase so it runs unconditionally on
every sprint story and every `forge run`, before any dev work begins.
