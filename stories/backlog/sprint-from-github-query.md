---
name: "Sprint from GitHub query — paperless sprint execution"
slug: sprint-from-github-query
github_issue: 253
pytest_target: tests/
---

# Sprint from GitHub Query

## Problem

Every sprint requires a manifest YAML that lists the stories to run. Even after
story-source-abstraction ships, the manifest still exists as a file that must be
created, committed, and maintained. For projects that use GitHub milestones and
labels as the source of truth for sprint planning, the manifest is pure ceremony
— it just mirrors what GitHub already knows.

There is no way to run `forge sprint` against a milestone or label query without
first writing a YAML file.

## Goal

`forge sprint` accepts a GitHub query — milestone name or label — as a direct
argument, builds the issue list from GitHub at runtime, and executes the sprint
without any manifest file on disk.

```bash
forge sprint --milestone "M3b: Plan review hardening" --budget 60
forge sprint --label sprint-ready --budget 40
```

Both manifest loading and GitHub query mode resolve to a common `ResolvedSprint`
runtime object (`name`, `budget_usd`, `stories`, `max_parallel`). The sprint
runner, daemon, and lock path all operate on the resolved object — no code path
assumes a manifest file exists. The manifest YAML format is unchanged and
remains fully supported.

## Acceptance Criteria

- Both manifest YAML loading and GitHub query mode produce a `ResolvedSprint`
  object; the sprint runner accepts that object — no path-shaped assumptions
  remain in `run_sprint()`, daemon slug derivation, or lock acquisition
- `forge sprint --milestone <name-or-number>` fetches all open issues in the
  named milestone, ordered by issue number, and runs them as the story list
- `forge sprint --label <label>` fetches all open issues with that label,
  ordered by issue number
- `--budget <usd>` is required when using any GitHub query mode and is rejected
  with a clear error if omitted
- Sprint name defaults to the milestone name or label when no `--name` is
  given; `--name` overrides
- Each fetched issue is resolved via `GitHubIssueSource`, with `Closes #N` in
  PR body and escalation comments on failure — identical to `issue: N` in a
  manifest
- Issues that are already closed at fetch time are skipped with a logged warning
- `depends_on` is not applicable in query mode; issues execute in issue-number
  order (sequential by default, parallel if `--parallel` is passed and no
  footprint overlap is detected)
- `forge sprint --milestone <name>` with no open issues exits cleanly with a
  message rather than erroring
- Existing `forge sprint <manifest.yaml>` behavior is unchanged — no regression
- `forge sprint --dry-run --milestone <name>` prints the resolved issue list
  and estimated story count without executing

## Out of Scope

- `--project --column` (project board queries) — deferred; project APIs are
  brittle and GraphQL v2 migration makes classic columns unreliable
- Persisting the query result as a manifest file (that's `forge sprint --dry-run`
  piped to a file by the user)
- Auto-ordering by priority label within a milestone (issue number order is
  deterministic and sufficient for v1)
- Cross-repo issue queries

## Notes

- `GitHubIssueSource` from story-source-abstraction is the resolution backend.
  This story adds the query layer that produces the list of issue numbers — it
  does not change how individual issues are fetched or closed.
- The core refactor is introducing `ResolvedSprint` as the common runtime shape.
  `load_sprint_manifest()` returns one, GitHub query mode returns one, and
  `run_sprint()` / daemon / lock code all accept one. This eliminates the
  manifest-path assumption that currently leaks through `runner.py`, `cli/sprint.py`,
  and `daemon.py` (slug derivation, lock key, queue submission all assume a file).
- `gh issue list --milestone <name> --state open --json number,title` is the
  fetch path for milestone queries. `--label <label>` uses
  `gh issue list --label <label> --state open --json number,title`.
- `--dry-run` should print a table of issue numbers and titles so the user can
  verify what will run before committing budget.
- This story prereqs story-source-abstraction being merged and `GitHubIssueSource`
  being functional.
