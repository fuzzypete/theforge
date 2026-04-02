---
name: "Story source abstraction — specs from GitHub issues, local files, or external trackers"
slug: story-source-abstraction
github_issue: 165
pytest_target: tests/
---

# Story Source Abstraction

## Problem

TheForge reads story specs exclusively from local markdown files. This creates a parallel tracking system alongside GitHub Issues, causing drift and duplication. The project already treats GH issues as the source of truth for priorities, but `forge sprint` can't read from them — so every issue has to be manually mirrored into a `.md` file before it can run.

## Goal

A `StorySource` abstraction so `forge sprint` can read specs from local files or GitHub issues interchangeably. Sprint manifests gain support for issue references:

```yaml
stories:
  - stories/backlog/some-local-story.md   # local file — current behavior unchanged
  - issue: 34                              # GitHub issue
  - issue: 130
```

The resolver picks the backend based on ref format. Local files work exactly as today.

## Acceptance Criteria

- `forge sprint` accepts a manifest with `issue: <number>` entries and executes them
- GitHub issue body is used as the story spec (passed to preflight, plan, dev, review)
- Issue closing is mode-dependent:
  - `on_approve: merge-pr` or `on_approve: pr` — PR body includes `Closes #N`; GitHub closes the issue when the PR merges. Coordinator does not call `gh issue close`.
  - `on_approve: merge` — no PR exists, so the coordinator calls `gh issue close` with a run-summary comment
  - `on_approve: ask` — same as `pr`; human merges, GitHub closes via keyword
- Issue receives a comment when the story escalates (with escalation reason), regardless of mode
- Local file stories continue to work unchanged — no regression
- `depends_on` in sprint manifest entries is respected for sequencing, consistent with existing DAG behavior
- Stories with overlapping file footprints (detected via preflight or plan output) emit a warning and run sequentially by default

## Out of Scope

- Jira, Linear, or other external tracker adapters (abstraction must allow them; don't build them)
- Auto-decomposition of large issues
- Migrating existing `stories/backlog/` files to GitHub issues

## Notes

- The abstraction point is a `StorySource` protocol with at minimum `fetch(ref) -> TaskStory` and `on_complete(ref, result)` / `on_escalate(ref, result)` callbacks. Keep the protocol minimal — don't over-engineer for backends that don't exist yet.
- `on_complete` is a no-op for PR-based modes (GitHub handles closing via the keyword). For `on_approve: merge` it calls `gh issue close` with a summary comment. The branching lives in the callback, not the coordinator.
- `GitHubIssueSource` uses `gh` CLI (already a hard dependency) to fetch issue body and post comments. No new auth surface.
- The `github_issue` field already exists on `TaskStory` — populate it from the issue number so PR creation gets `Closes #N` for free.
- Sprint manifest parsing lives in `src/theforge/sprint/manifest.py`. The resolver logic (file vs issue ref) belongs there or in a new `src/theforge/sprint/sources.py`.
- Overlap detection (shared file footprint → sequential execution) is a best-effort warning, not a hard block. False positives are acceptable; false negatives (missed conflicts) are annoying but recoverable.
