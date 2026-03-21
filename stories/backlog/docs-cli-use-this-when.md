---
name: Add opinionated "use this when" guidance to CLI reference
slug: docs-cli-use-this-when
pytest_target: tests/
---

# Add "Use This When" Guidance to CLI Reference

## Problem

The CLI reference documents flags and syntax but doesn't answer "when do I use
this?" Users get lost in feature surface area without opinionated guidance about
which command/flag fits their situation.

## Acceptance criteria

- Every major command in docs/guides/cli-reference.md has a short guidance block:
  - **Use this when:** one-line scenario
  - **Avoid this when:** one-line anti-pattern (where applicable)
  - **Pairs well with:** related flags or commands (where applicable)
- At minimum, guidance is added for:
  - `forge run` — end-to-end execution from story to review
  - `forge review` — review-only on existing worktree
  - `forge sprint` — multiple stories sequentially
  - `forge ideate` — generating stories from briefs
  - `--resume` — continuing after interruption or gate failure
  - `--verbose` — diagnosing provider, gate, or worktree behavior
  - `--auto-merge` — unattended runs
  - `--interactive` — human gate before merge
  - `--dry-run` — inspecting prompts without invoking agents
- Guidance is concise (1-2 lines per item), not paragraph explanations
