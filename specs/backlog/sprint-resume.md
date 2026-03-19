---
name: "sprint resume: auto-triage failed specs and pick optimal re-entry point"
slug: sprint-resume
file_scope:
  - src/theforge/sprint.py
  - src/theforge/coordinator.py
  - src/theforge/cli.py
  - tests/test_sprint.py
pytest_target: tests/
---

# Sprint Resume

## Problem

When a sprint finishes with failures, recovery requires manual human diagnosis:

1. Read sprint audit / logs to understand each failure
2. SSH into the machine, inspect each worktree
3. Determine recovery strategy per spec (restart? review only? skip?)
4. Issue individual `forge run` or `forge review` commands

This defeats the purpose of autonomous operation. Three of three HDP specs
had passing code in their worktrees but were reported as failures because
the gate failed due to missing fixtures — a workspace setup issue, not a
code issue. The human had to manually discover this and run `forge review`
per spec.

Forge should figure this out itself.

## Design

### `forge sprint --resume`

```bash
forge sprint sprints/quick-wins.yaml --resume --auto-merge
```

When `--resume` is passed, the sprint runner triages each spec before
executing it:

```
For each spec in the manifest:
  1. Was it already succeeded/merged in a prior run?
     → Skip (log: "already done")

  2. Does a worktree exist with commits ahead of base branch?
     Yes → Run the gate against the existing worktree code
       Gate passes → Enter at REVIEW (call run_from_review)
       Gate fails  → Enter at DEV (reuse worktree, skip WORKSPACE/PREFLIGHT)
     No  → Full run (WORKSPACE → PREFLIGHT → DEV → ...)

  3. Does a worktree exist with NO commits ahead?
     → Remove stale worktree, full run from scratch
```

### Triage decision tree

```
spec
 ├── merged to base branch? ──── yes ──→ SKIP ("already merged")
 ├── worktree exists?
 │    ├── commits ahead of base?
 │    │    ├── yes ──→ run gate
 │    │    │    ├── gate PASS ──→ REVIEW entry
 │    │    │    └── gate FAIL ──→ DEV entry (reuse worktree)
 │    │    └── no ──→ remove worktree, FULL run
 │    └── no worktree ──→ FULL run
```

### How "already merged" is detected

Check if the branch (`feat/{slug}`) has been merged to the base branch:

```bash
git branch --merged main | grep feat/{slug}
```

Or check if the worktree's HEAD is an ancestor of main:

```bash
git merge-base --is-ancestor feat/{slug} main
```

This is more reliable than reading the prior sprint audit (which might not
exist or could be from a different sprint run).

### How "gate passes" is checked

Run the project's gate command in the existing worktree:

```python
result = _run_gate(config, worktree_path)
if result.exit_code == 0:
    # Enter at REVIEW
else:
    # Enter at DEV
```

This reuses the existing `_run_subprocess` / gate infrastructure in the
coordinator. The gate timeout applies.

### Sprint audit continuity

Resume mode reads the prior sprint audit (if it exists) to carry forward
cost tracking. The resumed sprint's audit reflects cumulative costs across
all attempts.

---

## Requirements

### R1: `--resume` CLI flag

Add `--resume` flag to `forge sprint` subcommand in `cli.py`.

```bash
forge sprint sprints/manifest.yaml --resume [--auto-merge] [--no-notify]
```

Mutually compatible with all existing flags.

### R2: Triage function

New function in `sprint.py`:

```python
@dataclass
class SpecTriage:
    """Result of triaging a spec for sprint resume."""
    spec_path: str
    action: str  # "skip_merged", "review", "dev", "full"
    reason: str
    worktree_path: Path | None = None

def _triage_spec(
    spec_path: str,
    config: ForgeConfig,
    project_root: Path,
) -> SpecTriage:
    """Determine the optimal re-entry point for a spec."""
```

### R3: Merged detection

Check if `feat/{slug}` is already merged into the base branch using
`git merge-base --is-ancestor`. This is a git operation, not audit-file
dependent.

### R4: Gate pre-check

When a worktree exists with commits ahead, run the gate command against
it before deciding entry point. Use the existing gate infrastructure
(same command, same timeout, same handoff parsing).

### R5: Resume-aware sprint loop

Modify `run_sprint()` (or add `resume_sprint()`) to:
1. Triage each spec
2. Log the triage decision clearly
3. Call the appropriate coordinator entry point:
   - `skip_merged` → skip, count as succeeded
   - `review` → `run_from_review()`
   - `dev` → `run_task()` with existing worktree (skip WORKSPACE/PREFLIGHT)
   - `full` → `run_task()` normal path

### R6: Triage logging

Clear, human-readable log output:

```
[sprint] Resuming "Quick wins sprint"  3 specs
[sprint] Triaging specs...
  rest-timer:       REVIEW (worktree exists, gate passes)
  scroll-focus:     DEV (worktree exists, gate fails)
  grid-ordering:    SKIP (already merged to main)
```

### R7: Cost continuity

If a prior sprint audit exists at `.forge/audits/sprint-audit.yaml`,
read the `total_cost` and carry it forward. The resumed sprint's final
audit shows cumulative cost.

### R8: Non-resume backward compatibility

Without `--resume`, `forge sprint` behaves exactly as today. The triage
step is only activated by the flag.

### R9: Tests

- `test_triage_merged_spec`: branch merged to main → skip_merged
- `test_triage_worktree_with_passing_gate`: worktree + gate pass → review
- `test_triage_worktree_with_failing_gate`: worktree + gate fail → dev
- `test_triage_no_worktree`: no worktree → full
- `test_triage_stale_worktree_no_commits`: worktree 0 commits ahead → full
- `test_resume_sprint_skips_merged`: end-to-end resume skips merged specs
- `test_resume_sprint_enters_review`: end-to-end resume enters at review
- `test_resume_cost_continuity`: costs carried from prior audit
- `test_no_resume_flag_unchanged`: without --resume, behavior unchanged

---

## Acceptance Criteria

1. `forge sprint --resume` triages each failed spec automatically
2. Specs with passing code go straight to review (no wasted dev iterations)
3. Specs already merged to main are skipped
4. Specs with failing code re-enter at DEV with existing worktree
5. Specs with no worktree get a full run
6. Triage decisions logged clearly
7. Cumulative cost tracking across resume attempts
8. Without `--resume`, zero behavior change

## Out of Scope

- Automatic retry policies (e.g., "retry failed specs N times")
- Per-spec resume (resume only specific specs from a sprint)
- Sprint audit diffing (comparing before/after resume)
