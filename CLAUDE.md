# Claude Guidance for TheForge

TheForge is a deterministic multi-LLM development orchestrator. This file provides
conventions for any Claude agent working in this codebase.

---

## Architecture

**The coordinator (not an LLM) makes all process decisions.** Every state transition
is deterministic Python code. Agents only write code and write reviews. The coordinator
validates boundaries mechanically.

State machine: `INIT → WORKSPACE → DEV → VALIDATE → REVIEW → DONE/ESCALATE`

Key modules:
- `src/theforge/coordinator.py` — state machine, the heart of the system
- `src/theforge/runner.py` — subprocess invocation of agent CLIs
- `src/theforge/config.py` — forge.yaml parsing and model profiles
- `src/theforge/task.py` — prompt builders (dev + review)
- `src/theforge/review.py` — YAML review output parsing
- `src/theforge/schemas.py` — review schema validation
- `src/theforge/cli.py` — `forge` CLI entry point

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
spec_compliance:
  matches_spec: true | false
  mismatches: []
test_coverage:
  adequate: true | false
  gaps: []
```

### File scope in TaskSpec
`file_scope` restricts what the dev agent may modify. Empty list = no restriction.
The dev prompt will say "(no scope restriction — all project files)" when empty.

### Dogfooding config
`forge.yaml` at the project root configures theforge to develop itself. Worktrees
land in `.forge/worktrees/<slug>/` on branch `feat/<slug>`.

## Testing

- All tests must pass before committing
- New coordinator behaviour → test in `tests/test_coordinator.py`
- New runner behaviour → test in `tests/test_runner.py`
- Mock subprocess; never invoke real agent CLIs in tests

## What NOT to do

- Do NOT have the coordinator call an LLM for routing decisions
- Do NOT merge to main without a PASS gate + review APPROVE
- Do NOT skip `make fmt` before committing
- Do NOT relax schema validation to make tests pass
