---
name: "Rename campaign to sprint throughout"
slug: rename-campaign-to-sprint
file_scope:
  - src/theforge/campaign.py
  - src/theforge/cli.py
  - src/theforge/coordinator.py
  - tests/test_campaign.py
  - campaigns/
pytest_target: tests/
---

# Rename Campaign → Sprint

## Problem

`campaign` is hard to spell and doesn't match the mental model well.
A sprint is a bounded collection of stories/tasks — which is exactly
what a forge sprint manifest is.

## Requirements

### R1: Rename CLI subcommand

`forge campaign` → `forge sprint`

Keep `forge campaign` as a deprecated alias that prints a warning and
delegates to `forge sprint`. Remove the alias after one sprint cycle.

### R2: Rename `campaign.py` → `sprint.py`

Rename the module. Update all imports throughout the codebase.

### R3: Rename public API

| Before | After |
|--------|-------|
| `run_campaign()` | `run_sprint()` |
| `CampaignManifest` | `SprintManifest` |
| `CampaignResult` | `SprintResult` |
| `load_manifest()` | `load_sprint_manifest()` |

### R4: Rename manifest directory

`campaigns/` → `sprints/`

Move existing manifests:
- `campaigns/hardening.yaml` → `sprints/hardening.yaml`
- `campaigns/ux-polish.yaml` → `sprints/ux-polish.yaml`

Update any path references in docs and config.

### R5: Rename audit output

`campaign-audit.yaml` → `sprint-audit.yaml`

Update `run_sprint()` and `cmd_sprint()`.

### R6: Rename log prefixes

`[campaign]` → `[sprint]` in all log output.

### R7: Update vision.md and CLAUDE.md

Update all references to "campaign" in documentation:
- `docs/vision.md` Phase 7 section
- `CLAUDE.md` key commands section

### R8: Tests

Rename `tests/test_campaign.py` → `tests/test_sprint.py`.
Update all internal references. No new test logic needed — this is
purely a rename.

## Out of Scope

- Changing any sprint manifest schema (YAML keys stay the same for now)
- Renaming `forge_audit.yaml` (per-spec audit, not sprint-level)

## Acceptance Criteria

- [ ] `forge sprint sprints/hardening.yaml` runs a sprint
- [ ] `forge campaign` prints deprecation warning and works as alias
- [ ] `sprint-audit.yaml` written on completion
- [ ] Log output shows `[sprint]` prefix
- [ ] All existing tests pass (in renamed test file)
- [ ] `campaigns/` directory renamed to `sprints/`
