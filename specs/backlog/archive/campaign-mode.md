---
name: "Campaign mode for multi-spec execution"
slug: campaign-mode
file_scope:
  - src/theforge/campaign.py
  - src/theforge/cli.py
  - tests/test_campaign.py
pytest_target: tests/
---

# Campaign Mode

## Problem

Running specs one at a time is fine for development but doesn't scale.
Reaching vision completion requires executing multiple specs sequentially,
each through the full PREFLIGHT → DEV → VALIDATE → REVIEW pipeline,
with aggregate budget tracking and per-spec results.

## Context

### What exists

- `forge run specs/foo.md` runs a single spec through the state machine
- `run_task()` returns `CoordinatorResult` with full cost/outcome data
- PREFLIGHT catches already-done and blocked specs cheaply
- `--auto-merge` (once implemented) merges branches after APPROVE
- Budget enforcement works per-profile within a single run

### Design constraints (from review)

1. **Budget tracking is Claude-only.** Codex and Gemini runners report
   `cost_usd=0.0`. Campaign budget enforcement gates on Claude costs
   only. The campaign log should note this limitation prominently.
2. **No `--auto` overload.** `--auto-merge` is a separate flag from
   `--interactive`. Campaign mode uses `--auto-merge` to merge each
   spec's branch sequentially. Do not conflate skip-human-review with
   auto-merge.
3. **No DECOMPOSE.** Campaign specs are human-authored in a manifest
   file. LLM-driven task decomposition is a future phase. The campaign
   outer loop is fully deterministic.

## Requirements

### 1. Campaign manifest: `campaign.yaml`

A YAML file listing specs to execute in order:

```yaml
# campaign.yaml
name: "v0.3 feature batch"
budget_usd: 10.00  # aggregate ceiling (Claude costs only)
specs:
  - specs/archive/auto-merge.md
  - specs/archive/campaign-mode.md
  - specs/some-other-feature.md
```

Fields:
- `name`: human-readable campaign name (for audit)
- `budget_usd`: aggregate cost ceiling across all specs. Only Claude
  costs count toward this limit. A warning is logged noting this.
- `specs`: ordered list of spec file paths (relative to project root)

### 2. `forge campaign` CLI command

```bash
forge campaign campaign.yaml [--auto-merge] [--interactive]
```

- Reads the manifest
- Validates all spec paths exist and parse (fail fast before running anything)
- Runs each spec sequentially via `run_task()`
- `--auto-merge`: merge each spec's branch into base after APPROVE
- `--interactive`: pause for human review at each spec

### 3. Campaign runner: `src/theforge/campaign.py`

New module with:

```python
@dataclass
class CampaignResult:
    name: str
    specs_total: int
    specs_succeeded: int
    specs_failed: int
    specs_skipped: int  # ALREADY_DONE or budget-stopped
    total_cost_usd: float
    budget_usd: float
    results: list[tuple[str, CoordinatorResult]]  # (spec_path, result)
    stopped_reason: str | None  # why campaign stopped early, if it did

def run_campaign(
    config: ForgeConfig,
    manifest_path: Path,
    *,
    auto_merge: bool = False,
    interactive: bool = False,
) -> CampaignResult:
    ...
```

The outer loop:

```
for each spec in manifest.specs:
    1. Load TaskSpec from spec file
    2. Check aggregate cost against budget_usd → stop if exceeded
    3. Run run_task(config, task, interactive=interactive, auto_merge=auto_merge)
    4. Accumulate cost
    5. If result.success and auto_merge: branch is already merged by run_task
    6. If not result.success: log failure, continue to next spec
       (campaign does not stop on individual spec failure)
    7. Log per-spec summary to stderr
```

### 4. Budget enforcement

- Before each spec run, check `accumulated_cost + 0 < budget_usd`
  (we can't predict cost, but we can stop before starting if already over)
- After each spec run, add `result.state.total_cost` to accumulated cost
- If accumulated cost exceeds `budget_usd` after a run, stop the campaign
  and report remaining specs as skipped
- Log a warning at campaign start: "Budget enforcement tracks Claude costs
  only. Codex/Gemini invocations report $0.00."

### 5. Campaign audit

After completion, write `campaign-audit.yaml` to the project root:

```yaml
campaign:
  name: "v0.3 feature batch"
  budget_usd: 10.00
  total_cost_usd: 4.50
  budget_note: "Costs reflect Claude invocations only; Codex/Gemini report $0.00"
  started_at: "2026-03-12T10:00:00Z"
  finished_at: "2026-03-12T10:45:00Z"
  duration_seconds: 2700
  specs_total: 3
  specs_succeeded: 2
  specs_failed: 0
  specs_skipped: 1
  stopped_reason: null
specs:
  - path: specs/archive/auto-merge.md
    outcome: DONE
    cost_usd: 2.00
    preflight: PROCEED
    merge: true
  - path: specs/archive/campaign-mode.md
    outcome: DONE
    cost_usd: 2.50
    preflight: PROCEED
    merge: true
  - path: specs/some-feature.md
    outcome: ALREADY_DONE
    cost_usd: 0.15
    preflight: ALREADY_DONE
    merge: false
```

### 6. CLI output

During campaign execution, print progress to stderr:

```
[campaign] Starting "v0.3 feature batch" (3 specs, budget=$10.00)
[campaign] ⚠ Budget tracks Claude costs only (Codex/Gemini report $0.00)
[campaign] [1/3] specs/archive/auto-merge.md
  ... forge run output ...
[campaign] [1/3] DONE ($2.00, merged)
[campaign] [2/3] specs/archive/campaign-mode.md
  ... forge run output ...
[campaign] [2/3] DONE ($2.50, merged)
[campaign] [3/3] specs/some-feature.md
  ... forge run output ...
[campaign] [3/3] ALREADY_DONE ($0.15, skipped)
[campaign] Campaign complete: 2 succeeded, 0 failed, 1 skipped. Total: $4.65
```

## Out of Scope

- **DECOMPOSE**: LLM-driven spec decomposition. Campaign specs are
  human-authored. This is a future phase.
- **Parallel execution**: Specs run sequentially. Parallel worktree
  execution introduces merge conflicts.
- **Retry across specs**: If spec B depends on spec A and A fails,
  the campaign does not retry A. It logs the failure and moves on.

## Acceptance Criteria

- [ ] `campaign.yaml` manifest loads and validates (spec paths exist, budget > 0)
- [ ] `forge campaign campaign.yaml` runs specs sequentially via `run_task()`
- [ ] Aggregate budget check before each spec; stop campaign if exceeded
- [ ] `--auto-merge` passed through to each `run_task()` call
- [ ] `--interactive` passed through to each `run_task()` call
- [ ] Campaign continues after individual spec failure (logs and moves on)
- [ ] ALREADY_DONE specs counted as skipped, not failed
- [ ] `campaign-audit.yaml` written with per-spec outcomes and costs
- [ ] Budget warning logged at campaign start (Claude-only limitation)
- [ ] All existing tests continue to pass
- [ ] New tests in `tests/test_campaign.py` cover: success path, budget exceeded,
      spec failure continuation, ALREADY_DONE handling, manifest validation
