# Discovery: Product Development Workflow for TheForge

## Problem Statement

TheForge currently supports individual spec execution (`forge run`) and batched
spec execution (`forge sprint`). It lacks the upstream workflow that connects
product discovery → planning → sprint execution. This gap forces users to do
consolidation and workstream planning manually outside of forge.

## Current State

- `forge ideate` — produces a single discovery/spec doc from a brief (standalone)
- `forge sprint` — executes a ordered list of specs sequentially
- No consolidation across multiple discovery docs
- No workstream/track planning that sequences specs with dependencies
- User manually bridges ideation output → sprint manifest

## Desired Workflow (from HDP field use)

### Stage 1: Ideate (exists, partial)
- Multiple ideation sessions on related problem areas
- Each produces a discovery doc in `discovery/`
- Sessions can overlap; related docs accumulate over time
- Model: Opus (high reasoning, research-heavy)

### Stage 2: Synthesize (missing)
- `forge synthesize <discovery-docs...>` 
- Consolidates multiple related discovery docs into a single unified doc
- Resolves overlaps, surfaces dependencies, identifies gaps
- Output: consolidated discovery doc

### Stage 3: Deep Plan (missing)
- `forge plan <consolidated-discovery>`
- Produces a workstream track: ordered specs with dependencies
- Equivalent to an epic with child stories
- Output: a `tracks/<name>.yaml` with ordered spec list + rationale
- Model: Opus (translating discovery into actionable implementation plan)

### Stage 4: Sprint (exists)
- Multiple `forge sprint` runs chip away at the track
- Each sprint picks up where the last left off
- `forge sprint --resume` recovers failed specs (sprint-resume spec)

## Key Design Principles

- Discovery docs are durable artifacts, not ephemeral — they inform future planning
- Multiple ideations on similar areas should be first-class, not ad hoc
- Workstream tracks decouple planning from execution — plan once, sprint many times
- The workflow should support long-running projects (weeks/months), not just one-off specs

## Open Questions

- How does `forge synthesize` know which docs are "related"? User-specified or auto-clustered?
- Does `forge plan` produce specs automatically or just the track structure?
- How does sprint-resume interact with track-level progress tracking?
- Should tracks have their own budget/timeline tracking separate from individual sprints?

## Relationship to Existing Features

- `forge ideate` needs a `--output-dir` flag to route docs to `discovery/`
- `forge sprint --resume` is a prerequisite for reliable track execution
- Smart model config should route: ideate/plan → Opus, sprint → Sonnet+Codex
