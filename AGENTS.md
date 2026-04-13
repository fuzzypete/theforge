# Codex Guidance for TheForge

TheForge is a deterministic multi-LLM development orchestrator. This file provides
conventions for any Codex agent working in this codebase.

---

## Interactive Development Workflow

All interactive dev work — whether by a human or an agent in an interactive session —
**must follow this sequence without exception:**

1. **Create a GitHub issue** describing the change (story format: WHAT + WHY, not HOW).
   ```bash
   gh issue create --title "..." --body "..."
   ```

2. **Create a worktree and branch** tied to that issue number.
   ```bash
   git worktree add .forge/worktrees/issue-<N> -b feat/issue-<N>
   cd .forge/worktrees/issue-<N>
   ```

3. **Commit changes referencing the issue** so the audit trail is complete.
   ```bash
   git commit -m "fix: description (#<N>)"
   ```
   Every commit must reference the issue number. Do not commit directly to main.

4. **Produce a handoff for Review** — run `make gate` and report the result.
   ```bash
   make gate   # runs tests + writes .forge/handoff.yaml
   ```
   The handoff is the exit artifact. Work is not done until it exists and tests pass.

**None of these steps are optional.** Starting to code before the issue exists, or
finishing without a handoff, leaves work unreviewed and untracked.

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

## Pipeline Phases

### Preflight is a reasoning task, not a cheap classifier

Preflight looks like a classifier (structured YAML output, one-shot call) but the
work is heavy. It must:

1. **Read the codebase** and verify every acceptance criterion against actual code
   to determine PROCEED / ALREADY_DONE / BLOCKED
2. **Assess complexity** (small/medium/large) — this drives adaptive model selection
   for all downstream phases
3. **Classify sufficiency** (implementation_ready/needs_planning) — controls whether
   the plan phase runs at all
4. **Classify work type** (feature/refactor/mechanical/bug) — feeds prompt construction
5. **Drive adaptive assignment** — complexity feeds `assign_models()` which picks
   agent tiers, escalation history, reviewer pool selection

A wrong ALREADY_DONE wastes a correct implementation. A wrong PROCEED on finished
work burns $20+ on dev+review for nothing. A wrong complexity classification puts
the wrong model on the job. **Do not suggest replacing preflight with a cheap/fast
model.** The current DeepSeek-reasoner config is intentional — $0.30 for a careful
classification that controls $20-50 of downstream spend is correct.

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
- **Never use `fcntl.flock` in tests that also use `threading`.** pytest runs with `-n auto --dist worksteal` (xdist), which forks worker processes. A forked worker inherits open file descriptors with held locks, causing sibling threads to block indefinitely — deadlock, memory balloon, and eventual OOM. Mock the lock instead.
- **Never write tests that can hang.** No `while True`, no unbounded retry loops, no `time.sleep()` longer than 1 second, no blocking I/O without a timeout, no `threading.Event.wait()` without a timeout. Every test must complete in under 5 seconds. A hanging test kills the entire gate run for every story in the sprint.
- **Never import optional provider SDKs unconditionally in tests.** Tests must pass whether the environment has `.[dev]` or `.[all,dev]` installed. Mock or stub provider SDK boundaries.

## Cutting a Release

The full release process is documented in [`RELEASING.md`](RELEASING.md). Do not
cut a release without reading it. Key points:

- Update `CHANGELOG.md` (`[Unreleased]` → `[X.Y.Z] — date`)
- Bump `pyproject.toml` version to match the tag
- Tag and push **before** bumping back to `X.Y.Z+1.dev0`
- Hotfixes branch from `release/vX.Y`, not `main`

## What NOT to do

- Do NOT have the coordinator call an LLM for routing decisions
- Do NOT merge to main without a PASS gate + review APPROVE
- Do NOT skip `make fmt` before committing
- Do NOT relax schema validation to make tests pass
- Do NOT suggest replacing preflight with a cheap/fast model — it is load-bearing
- Do NOT modify CLAUDE.md or AGENTS.md unless the story explicitly requires it
