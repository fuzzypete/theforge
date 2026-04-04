# Codex Guidance for TheForge

TheForge is a deterministic multi-LLM development orchestrator. This file provides
conventions for any Codex agent working in this codebase.

---

## Architecture

**The coordinator (not an LLM) makes all process decisions.** Every state transition
is deterministic Python code. Agents only write code and write reviews. The coordinator
validates boundaries mechanically.

State machine: `INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW → DONE/ESCALATE`

Key modules:
- `src/theforge/coordinator/engine.py` — state machine, the heart of the system
- `src/theforge/coordinator/` — all coordinator phases (dev_phase, review_phase, validate_phase, plan_flow, preflight, workspace, etc.)
- `src/theforge/runners/` — API and CLI agent runners; adapters per provider
- `src/theforge/config/` — forge.yaml parsing and model profiles
- `src/theforge/task/` — prompt builders (dev, review, plan)
- `src/theforge/review.py` — review output parsing
- `src/theforge/schemas.py` — review schema validation
- `src/theforge/cli/main.py` — `forge` CLI entry point
- `src/theforge/sprint/` — sprint lifecycle, DAG scheduler, GitHub query

## Key Commands

```bash
make fmt        # ruff format + ruff check --fix (auto-fix)
make lint       # ruff check + ruff format --check (no auto-fix)
make test       # pytest tests/ -v
make gate       # run tests + write handoff.yaml
```

## Conventions

### No LLM in the loop for process decisions
The coordinator is pure Python. If you find yourself writing code where an LLM
decides whether to retry or escalate, stop — that decision belongs in the coordinator.

### Schema enforcement is mandatory
The review output schema in `schemas.py` is the integrity boundary. Do not relax
cross-validation rules (APPROVE+P1 or REQUEST_CHANGES+no P1 are always errors).

### Review YAML structure
```yaml
verdict: APPROVE | REQUEST_CHANGES
summary: "<one-line>"
findings:
  - severity: P1 | P2
    file: "<path>"
    line: <number or null>
    description: "<what is wrong>"
    suggestion: "<how to fix>"
story_compliance:
  matches_spec: true | false
  mismatches: []
test_coverage:
  adequate: true | false
  gaps: []
```

### Stories
Stories are GitHub issues — there are no local story files. The primary term is
"story" throughout the codebase. `TaskSpec` is a backward-compat alias for
`TaskStory`; prefer `TaskStory` in new code.

### Dogfooding config
`forge.yaml` at the project root configures theforge to develop itself. Worktrees
land in `.forge/worktrees/<slug>/` on branch `feat/<slug>`.

## Testing

- All tests must pass before committing
- New coordinator behaviour → add a `tests/test_coord_*.py` file matching the phase
- New runner behaviour → `tests/test_runner_*.py`
- Mock subprocess; never invoke real agent CLIs in tests

## What NOT to do

- Do NOT have the coordinator call an LLM for routing decisions
- Do NOT merge to main without a PASS gate + review APPROVE
- Do NOT skip `make fmt` before committing
- Do NOT relax schema validation to make tests pass
