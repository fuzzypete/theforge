# Claude Guidance for TheForge

TheForge is a deterministic multi-LLM development orchestrator. This file provides
conventions for any Claude agent working in this codebase.

Directory-level `CLAUDE.md` files under `src/theforge/` provide subsystem-local
context. When working inside `coordinator/`, `runners/`, `sprint/`, `task/`,
`config/`, or `cli/`, read the nearest local `CLAUDE.md` in addition to this
root guide. Treat local `Invariants` sections as hard constraints and `Context`
sections as navigational guidance.

---

## Current State — Start Here

To understand what's in progress and what's next, run:
```bash
gh milestone list                              # see milestones
gh issue list --milestone "v0.9.0"             # current priority after v0.8.0
gh project item-list 1 --owner fuzzypete       # full project board
```

Project board: https://github.com/users/fuzzypete/projects/1

Stories are GitHub issues — there are no local story files. GH milestones + issues
are the single source of truth for priorities, status, and story content.

---

## Interactive Development Workflow

All interactive dev work — whether by a human or Claude in an interactive session —
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

4. **Verify the gate passes** — run `make gate` locally to confirm lint, format, and
   tests all pass (exit 0). Print the summary here in the CLI session.
   ```bash
   make gate   # lint + format check + pytest; passes on exit 0
   ```
   Work is not done until `make gate` exits 0 and all tests pass.

**None of these steps are optional.** Starting to code before the issue exists, or
finishing without a passing gate, leaves work unreviewed and untracked.

---

## Directory-level guidance

Directory-level `CLAUDE.md` files under `src/theforge/` provide subsystem-specific guidance for major areas of the codebase. Consult them before making changes in those directories, especially:
- `coordinator/`
- `runners/`
- `sprint/`
- `task/`
- `config/`
- `cli/`

## Architecture

**The coordinator (not an LLM) makes all process decisions.** Every state transition
is deterministic Python code. Agents only write code and write reviews. The coordinator
validates boundaries mechanically.

State machine: `INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW → DONE/ESCALATE`

Key modules:
- `src/theforge/coordinator/engine.py` — state machine, the heart of the system
- `src/theforge/coordinator/` — all coordinator phases (dev_phase, review_phase, validate_phase, plan_flow, preflight_flow, workspace, etc.)
- `src/theforge/runners/` — API and CLI agent runners; adapters per provider
- `src/theforge/config/` — forge.yaml parsing and model profiles
- `src/theforge/task/` — prompt builders (dev, review, plan, preflight)
- `src/theforge/review.py` — review output parsing
- `src/theforge/schemas.py` — review schema validation
- `src/theforge/cli/main.py` — `forge` CLI entry point
- `src/theforge/sprint/` — sprint lifecycle, DAG scheduler, GitHub query

## Key Commands

```bash
make fmt        # ruff format + ruff check --fix (auto-fix)
make lint       # ruff check + ruff format --check (no auto-fix)
make test       # pytest tests/ -v
make gate       # lint + format check + tests (exit code only, no file written)
```

### Language and toolchain agnosticism
TheForge is a generic orchestrator — it must work for Python, Node, Go, Java, Rust,
or any other stack. Coordinator logic, prompt templates, task schemas, and CLI
scaffolding must not assume a specific language, test framework, or build tool.

#### Concrete convention rules
- **Core orchestrator modules must be stack-neutral.** Code in shared coordinator,
  task, sprint, and related config layers must not bake in assumptions about one
  language, package manager, test runner, or repository layout.
- **Shared schemas may not encode stack-specific concepts.** `TaskStory`,
  `ForgeConfig`, coordinator state, and other shared models must not introduce
  fields like `pytest_target`, `npm_script`, or similar stack-shaped concepts.
  Use generic names such as `test_target`, `gate_command`, and
  `gate_debug_command`.
- **Prompt templates must reference configured commands, not literal tool
  invocations.** Reusable prompts should talk about the configured gate/test
  commands rather than embedding `make fmt`, `pytest`, `npm test`, `cargo test`,
  or `go test`.
- **Generated scaffolding must use generic names or omit the concept.** Reusable
  examples and templates should prefer neutral placeholders like `test_target`
  instead of assuming `tests/`, `src/`, `docs/`, or a language-specific layout.
- **Stack-specific assumptions belong in `forge.yaml` or repo-local conventions,
  not TheForge core.** Repo-local dogfooding config, self-hosting examples, and
  clearly marked stack-specific docs may be specific; shared orchestrator code may
  not.

#### Reviewer smell list
Treat the following as concrete smells in stack-neutral layers:
- Shared models with `pytest_`, `npm_`, `cargo_`, `maven_`, or `gradle_` prefixes
- Core prompt templates containing literal `make fmt`, `pytest`, `npm test`,
  `cargo test`, or `go test`
- Reusable prompt logic that hardcodes `src/`, `tests/`, or `docs/`
- Language-specific story parsing in shared orchestrator code

#### Mechanical enforcement scope
The hard conventions check scans only stack-neutral layers:
- `src/theforge/task/`
- `src/theforge/coordinator/`
- `src/theforge/sprint/`
- shared schema modules
- relevant shared config modules under `src/theforge/config/`

It intentionally exempts repo-local dogfooding config such as `forge.yaml`,
provider/adapter code, migration tests that mention old names, and docs/examples
that are clearly marked as Python-specific examples.

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

### Coordinator seam changes require integration tests
Changes that affect coordinator phase boundaries, state handoff between phases, or adaptive routing/config propagation must include seam-level integration tests covering the touched boundary. Unit tests alone are insufficient when correctness depends on cross-phase state flow.

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

### Writing stories
Stories describe WHAT and WHY — never HOW. The plan phase produces the HOW.

- **No function names, class names, or file paths** unless the story IS about
  a specific file (e.g., a refactoring story). The plan agent will find these.
- **Acceptance criteria describe observable behavior**, not implementation steps.
  "Warns on unmapped acceptance criteria" ✓. "Calls `validate_plan()` in
  `coordinator.py` after line 1460" ✗.
- **If preflight can't understand a story without reading the codebase, the
  story is too implementation-coupled.** Preflight should be able to classify
  it from the story text alone.
- **`## Notes` is for soft hints, not requirements.** Use a Notes section to
  capture file paths, patterns, or gotchas discovered during investigation.
  Notes are informational — they may be stale or wrong by the time the story
  runs. Agents are instructed to verify Notes against the codebase. Never put
  acceptance criteria or requirements in Notes.
- The primary term is "story" throughout the codebase. `TaskSpec` is a
  backward-compat alias for `TaskStory`; prefer `TaskStory` in new code.

### Writing bug reports
Bug reports contain exactly two things:

1. **What happened** — the observed behavior, with evidence (log lines, audit
   trail entries, run IDs).
2. **What was expected** — the correct behavior.

That's it. No acceptance criteria, no implementation hints, no file paths, no
suggestions about which module or function to fix. The dev agent should discover
the fix from the codebase. Over-constraining the report biases the agent toward
a specific fix path that may not be the right one.

### Dogfooding config
`forge.yaml` at the project root configures theforge to develop itself. Worktrees
land in `.forge/worktrees/<slug>/` on branch `feat/<slug>`.

## Testing

- All tests must pass before committing
- New coordinator behaviour → add a `tests/test_coord_*.py` file matching the phase
- New runner behaviour → `tests/test_runner_*.py`
- `make gate` runs in a scrubbed environment: agent credentials, CLI auth state, and dotenv autoload inputs are stripped before tests execute.
- Mock subprocess; never invoke real agent CLIs in tests. Any forgotten runner/provider mock in the default gate suite should fail fast under the gate scrub sentinel.
- Tests that legitimately require real credentials must be marked `@pytest.mark.network_integration` and run via `make test-integration`; they are not part of `make gate`.
- **Never use `fcntl.flock` in tests that also use `threading`.** pytest runs with
  `-n auto --dist worksteal` (xdist), which forks worker processes. A forked worker
  inherits open file descriptors with held locks, causing sibling threads to block
  indefinitely — deadlock, memory balloon, and eventual OOM. Mock the lock instead.
- **Never write tests that can hang.** No `while True`, no unbounded retry loops,
  no `time.sleep()` longer than 1 second, no blocking I/O without a timeout, no
  `threading.Event.wait()` without a timeout. Every test must complete in under 5
  seconds. A hanging test kills the entire gate run for every story in the sprint.

## Cutting a Release

The full release process is documented in [`RELEASING.md`](RELEASING.md). Use the
script — do not run steps manually:

```bash
scripts/release.sh X.Y.Z          # release
scripts/release.sh --dry-run X.Y.Z # preview
```

Key points:
- Verify the CHANGELOG release section against the milestone and commit range
  before tagging; GitHub release notes are generated from that section.
- Tag and push **before** bumping back to `X.Y.Z+1.dev0`
- Hotfixes branch from `release/vX.Y`, not `main`

## What NOT to do

- Do NOT have the coordinator call an LLM for routing decisions
- Do NOT merge to main without a PASS gate + review APPROVE
- Do NOT skip `make fmt` before committing
- Do NOT relax schema validation to make tests pass
- Do NOT suggest replacing preflight with a cheap/fast model — it is load-bearing
- Do NOT modify CLAUDE.md or AGENTS.md unless the story explicitly requires it
