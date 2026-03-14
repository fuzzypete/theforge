---
name: "Spec dependency analysis for campaign parallelization"
slug: dependency-analysis
file_scope:
  - src/theforge/deps.py
  - src/theforge/campaign.py
  - tests/test_deps.py
pytest_target: tests/
---

# Spec Dependency Analysis

## Problem

Campaign mode runs specs sequentially. Parallel execution would cut wall-clock
time dramatically, but running specs with overlapping `file_scope` concurrently
produces merge conflicts. Before we can parallelize, we need a static analysis
step that determines which specs are independent.

## Context

### What exists

- `campaign.yaml` manifests list specs to run in order
- Each spec has `file_scope` in its frontmatter (list of file paths the agent
  may modify). Empty list = unrestricted (may touch anything).
- `_build_task_from_spec()` in `campaign.py` already parses frontmatter and
  extracts `file_scope`
- `TaskSpec.file_scope` is `list[str]` — relative paths like
  `src/theforge/coordinator.py`

### What's missing

No mechanism to compare `file_scope` across specs and determine which can
safely run concurrently. The campaign runner has no concept of batching.

## Design

### New module: `src/theforge/deps.py`

This module performs static analysis of spec file scopes to produce an
execution plan.

#### Core types

```python
@dataclass(frozen=True)
class SpecNode:
    """A spec with its resolved file scope."""
    spec_path: str          # relative path to spec file
    slug: str
    file_scope: frozenset[str]  # resolved file paths
    unrestricted: bool      # True if file_scope was empty

@dataclass(frozen=True)
class ExecutionPlan:
    """Ordered batches of specs. Specs within a batch are independent."""
    batches: list[list[SpecNode]]  # outer=sequential, inner=parallel
    conflicts: list[tuple[str, str, set[str]]]  # (spec_a, spec_b, overlapping_files)
```

#### `build_dependency_graph(spec_paths: list[Path]) -> ExecutionPlan`

1. Parse frontmatter from each spec to extract `file_scope`
2. Build a `SpecNode` for each spec
3. Compute pairwise conflicts:
   - Two specs conflict if their `file_scope` sets intersect
   - A spec with empty `file_scope` (unrestricted) conflicts with everything
4. Build batches using greedy graph coloring:
   - Treat specs as nodes, conflicts as edges
   - Greedily assign specs to the earliest batch where they have no conflicts
   - This is not optimal but is simple, deterministic, and sufficient
5. Return `ExecutionPlan` with batches and conflict annotations

#### `analyze_manifest(manifest_path: Path, project_root: Path) -> ExecutionPlan`

Convenience function: loads a campaign manifest, resolves spec paths,
calls `build_dependency_graph()`.

#### Directory-level conflict detection

`file_scope` entries may be directories (e.g. `src/theforge/`) or files.
Two scopes conflict if:
- Exact path match
- One is a prefix of the other (directory contains file)
- Both are files in the same directory AND either scope lists the directory

For simplicity in this phase: normalize all paths and use startswith checks.
`src/theforge/coordinator.py` conflicts with `src/theforge/` but not with
`src/theforge/cli.py`.

### Integration with campaign.py

Add an `analyze` step to `run_campaign()`:

```python
def run_campaign(config, manifest_path, *, auto_merge=False, interactive=False):
    manifest = load_manifest(manifest_path)
    spec_paths = _validate_spec_paths(manifest, config.project_root)

    # NEW: analyze dependencies and log execution plan
    plan = build_dependency_graph(spec_paths)
    _log_execution_plan(plan)

    # Continue with sequential execution (parallel comes in Phase 10)
    for idx, spec_path in enumerate(spec_paths, start=1):
        ...
```

The execution plan is logged but not acted on yet — Phase 10 will use it
to actually parallelize. This phase is purely analysis + reporting.

Add execution plan to campaign-audit.yaml:

```yaml
campaign:
  execution_plan:
    batches:
      - batch: 1
        specs: [specs/foo.md, specs/bar.md]
        note: "independent (disjoint file_scope)"
      - batch: 2
        specs: [specs/baz.md]
        note: "conflicts with foo on src/theforge/coordinator.py"
    total_batches: 2
    parallelizable: true  # >1 spec in at least one batch
```

### Warnings

- **Unrestricted specs**: If any spec has empty `file_scope`, log a warning:
  `"⚠ specs/foo.md has no file_scope — conflicts with all other specs"`
  Unrestricted specs are placed alone in their own batch.
- **Fully sequential**: If every spec conflicts with every other spec, log:
  `"All specs have overlapping file_scope — no parallelization possible"`

## Acceptance Criteria

1. `build_dependency_graph()` correctly identifies conflicts from overlapping
   `file_scope` entries
2. Specs with disjoint `file_scope` are grouped in the same batch
3. Specs with empty `file_scope` conflict with everything
4. Directory-level prefixes are detected as conflicts
5. `ExecutionPlan` is deterministic given the same inputs
6. Campaign audit includes the execution plan
7. Campaign log output shows batch structure
8. No change to actual execution order (still sequential) — this is
   analysis only

## Test Expectations

In `tests/test_deps.py`:

- **Disjoint scopes**: 3 specs with non-overlapping files → 1 batch of 3
- **Full overlap**: 3 specs all touching the same file → 3 batches of 1
- **Partial overlap**: A conflicts with B, B conflicts with C, A independent
  of C → batch 1: [A, C], batch 2: [B]
- **Unrestricted spec**: spec with empty file_scope → alone in its own batch
- **Directory prefix**: `src/theforge/` conflicts with `src/theforge/cli.py`
- **No specs**: empty list → empty execution plan
- **Single spec**: always 1 batch of 1

In `tests/test_campaign.py`:

- Campaign audit includes `execution_plan` section
- Campaign log output includes batch summary

## Out of Scope

- Actually running specs in parallel (Phase 10)
- Modifying execution order based on the plan (Phase 10)
- LLM-driven scope inference for specs without `file_scope`
- Cross-spec data dependencies (only file conflicts matter)
