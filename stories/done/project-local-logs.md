---
name: "Project-local log directory with per-story trace artifacts"
slug: project-local-logs
pytest_target: tests/
---

# Project-Local Log Directory

## Problem

Logs and trace artifacts currently land in `~/.forge/logs/<project>/forge.log`
(a single structured event stream) with no per-story breakdown. Worktrees
contain `forge_audit.yaml` and reviewer outputs, but worktrees are ephemeral —
once cleaned up, all detail is lost. There is no durable place to answer
"what did DeepSeek actually say when it reviewed this story?"

## Solution

Move all logging, tracing, and audit artifacts to `<project_root>/.forge/logs/`
with a directory-per-story structure. Every artifact survives worktree removal.

## Directory layout

```
<project_root>/.forge/
├── logs/
│   ├── forge.log                              # high-level structured event stream
│   ├── <story-slug>/
│   │   ├── run-<run_id>.log                   # per-run verbose tee (stderr mirror)
│   │   ├── dev-iter-<N>-<profile>.log         # raw agent stdout/stderr per dev invocation
│   │   ├── review-cycle-<N>/
│   │   │   ├── <reviewer-profile>.yaml        # raw reviewer output (pre-parse)
│   │   │   ├── <reviewer-profile>.yaml        # one file per pool member
│   │   │   └── synthesized.yaml               # final merged/synthesized review
│   │   ├── plan.md                            # approved plan snapshot
│   │   ├── plan-review/
│   │   │   ├── <reviewer-profile>.yaml        # raw plan reviewer outputs
│   │   │   └── synthesized.yaml
│   │   ├── preflight.yaml                     # preflight verdict + reasoning
│   │   └── audit.yaml                         # final forge_audit.yaml copy
│   └── <sprint-name>/
│       ├── sprint-summary.yaml                # sprint-level cost/timing/outcomes
│       └── <story-slug>/                      # same per-story structure
└── worktrees/                                 # ephemeral, can be cleaned
```

## Behaviour

### forge.log stays global
`forge.log` remains the structured JSONL event stream at `.forge/logs/forge.log`
(moved from `~/.forge/logs/<project>/`). It continues to receive all events.
The per-story directories hold the *detail* that forge.log summarises.

### Per-story directory created at WORKSPACE phase
When `run_task` creates a worktree, it also creates `.forge/logs/<slug>/`.
If running inside a sprint, the path is `.forge/logs/<sprint-name>/<slug>/`.

### Artifacts captured

| Artifact | When written | Source |
|---|---|---|
| `run-<run_id>.log` | Continuously during run | stderr tee (existing `run-log-capture`) |
| `dev-iter-<N>-<profile>.log` | After each dev invocation | runner stdout/stderr capture |
| `review-cycle-<N>/<profile>.yaml` | After each reviewer returns | raw reviewer output before YAML parsing |
| `review-cycle-<N>/synthesized.yaml` | After synthesis | merged review result |
| `plan.md` | After PLAN_REVIEW approves | copy of approved plan |
| `plan-review/<profile>.yaml` | After each plan reviewer | raw plan reviewer output |
| `preflight.yaml` | After PREFLIGHT | verdict, reasoning, cost |
| `audit.yaml` | At run_end | copy of worktree's forge_audit.yaml |

### Sprint structure
When invoked via `forge sprint`, stories nest under the sprint name:
`.forge/logs/my-sprint/story-a/`, `.forge/logs/my-sprint/story-b/`, etc.
The sprint-level `sprint-summary.yaml` aggregates cost/timing/outcomes.

### Backward compatibility
- `~/.forge/logs/<project>/forge.log` symlinks to `<project>/.forge/logs/forge.log`
  for one release cycle, then removed
- `forge_audit.yaml` in the worktree continues to be written (worktree-local
  copy), but the canonical copy lives in `.forge/logs/<slug>/audit.yaml`

## Changes Required

### `src/theforge/coordinator.py`
- `_begin_run_log_tee()`: write to `.forge/logs/<slug>/run-<run_id>.log` instead of
  `~/.forge/logs/<project>/<slug>-<run_id>.log`
- After each dev invocation: capture stdout/stderr to `dev-iter-<N>-<profile>.log`
- After each reviewer: write raw output to `review-cycle-<N>/<profile>.yaml`
- After review synthesis: write `synthesized.yaml`
- After PLAN approval: copy `plan.md`
- At `run_end`: copy `forge_audit.yaml` to `.forge/logs/<slug>/audit.yaml`

### `src/theforge/coord_phases.py`
- Review phase: capture raw reviewer output before parsing
- Plan review phase: capture raw plan reviewer output
- Preflight phase: write `preflight.yaml`

### `src/theforge/logger.py` (or wherever StructuredLogger lives)
- `_log_path` derived from project root `.forge/logs/forge.log`, not `~/.forge/logs/`
- Config migration: if `~/.forge/logs/<project>/forge.log` exists and project-local
  doesn't, move it

### `src/theforge/sprint.py`
- Create sprint-level directory `.forge/logs/<sprint-name>/`
- Pass sprint name to `run_task` so story dirs nest correctly
- Write `sprint-summary.yaml` at sprint completion

### `src/theforge/config.py`
- `log.log_file` default changes from `~/.forge/logs/{project}/forge.log` to
  `.forge/logs/forge.log` (relative to project root)

## Acceptance Criteria

- [ ] All log and trace artifacts land under `<project_root>/.forge/logs/`
- [ ] forge.log moves from `~/.forge/logs/<project>/` to `<project>/.forge/logs/`
- [ ] Per-story directory created with slug name (or sprint/slug for sprint runs)
- [ ] Raw reviewer YAML outputs stored per cycle, per reviewer profile
- [ ] Plan reviewer outputs stored in `plan-review/` subdirectory
- [ ] Approved plan snapshot stored as `plan.md`
- [ ] Preflight verdict stored as `preflight.yaml`
- [ ] `audit.yaml` copied to per-story log dir at run completion
- [ ] Dev agent stdout/stderr captured per iteration
- [ ] Sprint runs nest stories under sprint name directory
- [ ] `sprint-summary.yaml` written at sprint completion
- [ ] Worktree cleanup does NOT remove `.forge/logs/` artifacts
- [ ] All existing tests pass
- [ ] `.gitignore` updated: `.forge/logs/` ignored (artifacts are local, not committed)
