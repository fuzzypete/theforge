---
name: "GitHub Issues findings hook — reference post_run implementation"
slug: findings-gh-hook
pytest_target: tests/
---

# GitHub Issues Findings Hook

## Problem

Lifecycle hooks (`post_run`, `post_merge`, `post_sprint`) are now infrastructure,
but there's no reference implementation showing how to use them. Teams don't know
what a hook script looks like, how to parse the payload, or what's possible.

The highest-value integration is filing GitHub Issues for unresolved findings
when a story ESCALATES or APPROVES with P2s. This makes review findings
actionable instead of buried in audit YAML.

## Solution

Ship a reference `post_run` hook script that creates GitHub Issues from findings,
plus a `forge init-hooks` command that scaffolds it into a project.

## Implementation

### `forge init-hooks` command

New CLI subcommand that creates `.forge/hooks/` directory with the reference
scripts and a README explaining the payload format.

- Creates `.forge/hooks/post_run.sh` — the GitHub Issues script
- Creates `.forge/hooks/README.md` — documents payload schema and hook contract
- Prints guidance on adding `hooks:` block to `forge.yaml`

### `.forge/hooks/post_run.sh` (reference script)

```bash
#!/usr/bin/env bash
# Reference post_run hook: file GitHub Issues for findings
# Requires: gh CLI authenticated, jq
```

The script:
- Reads JSON payload from stdin (already provided by lifecycle-hooks infra)
- Filters by event type (`escalate` or `approve` with findings)
- For each P1/P2 finding, calls `gh issue create` with:
  - Title: `[P1] slug: description` (truncated)
  - Body: story slug, branch, verdict, file/line, description, suggestion
  - Labels: `forge-finding`, severity label (`p1` or `p2`)
- Handles `gh` not installed gracefully (warn, don't crash)
- Handles no findings gracefully (exit 0, no issues created)

### Documentation updates

- Update `docs/guides/getting-started.md` with hooks section
- Update `examples/hello-forge/forge.yaml` with commented-out hooks block

## Acceptance criteria

- [ ] `forge init-hooks` creates `.forge/hooks/post_run.sh` with executable permissions
- [ ] `forge init-hooks` creates `.forge/hooks/README.md` documenting the payload schema
- [ ] `forge init-hooks` prints guidance for adding hooks config to forge.yaml
- [ ] `forge init-hooks` is idempotent (doesn't overwrite existing hooks)
- [ ] Reference script parses the post_run JSON payload correctly
- [ ] Reference script creates GitHub Issues with title, body, and labels
- [ ] Reference script handles missing `gh` CLI gracefully (warn + exit 0)
- [ ] Reference script handles zero findings gracefully (exit 0)
- [ ] Unit tests for `cmd_init_hooks` (mocks filesystem)
- [ ] All existing tests pass
