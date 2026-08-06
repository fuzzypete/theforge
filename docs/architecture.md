# TheForge Architecture

One place to get the whole picture before (or instead of) reading seven ADRs.
Everything here is as-built on the current release; each section links the
authoritative source.

## The one-sentence model

Deterministic Python orchestrates; LLMs work inside bounded roles; every
decision that matters — retry, gate, escalate, route, refuse — is made by code
against recorded evidence, never by a model.

## Two lifecycles

TheForge operates at two levels. The **sprint** level decides *what runs*;
the **story** level decides *whether one piece of work lands*.

### Sprint level: intake → schedule → run → record

```text
GitHub issues (or story files)
  │  forge shape / diagnose / groom / todo      (intake tooling)
  ▼
Shape gate + admissibility                       src/theforge/sprint/shape_gate.py,
  │  refuses unready work with labeled,         src/theforge/admissibility.py
  │  machine-readable skip codes
  ▼
DAG scheduling                                   src/theforge/sprint/dag.py
  │  explicit depends_on edges + automatic
  │  collision-derived edges (preflight          src/theforge/sprint/collision.py
  │  likely_files overlap)
  ▼
Parallel story dispatch under one budget         src/theforge/sprint/runner.py
  │  each story gets its own coordinator run
  │  in an isolated git worktree
  ▼
Merge / PR + audit publication                   src/theforge/coordinator/completion.py
     base-branch audit commits are pushed and
     verified, never left silently local
```

- **Refusal is a feature, not an error.** An issue that fails the shape gate
  is skipped with a durable, queryable reason (`forge audits skips`), not
  guessed at. Doctrine: [Refusal as Capability](vision/refusal-capability.md);
  workflow: [ADR-0001](adr/0001-intake-readiness-workflow.md).
- Bugs are typed: a symptom bug must carry a `## Diagnosis` body section
  (landed by `forge diagnose` or by hand) before it is fix-ready. See
  [Bug shape](reference/bug-shape.md).

### Story level: the coordinator state machine

```text
INIT -> WORKSPACE -> PREFLIGHT -> PLAN -> PLAN_REVIEW
  -> DEV -> VALIDATE -> REVIEW -> DONE / ESCALATE
```

(plus `HUMAN_REVIEW` when the interactive checkpoint is enabled, and
`MERGE_FAILED` when auto-merge cannot land a story — the full `Phase` enum is
in `src/theforge/coordinator/state.py`.)

- **PREFLIGHT** emits a 1–10 `complexity_score` (small/medium/large bands are
  a derived compat shim) and `likely_files`, which feed routing and DAG edges.
- **PLAN / PLAN_REVIEW** produce and gate a structured plan before any code
  is written (`coordinator/plan_flow.py`).
- **DEV** iterates against **VALIDATE** (your gate command — tests, lint,
  whatever the repo declares); the coordinator, not the model, counts
  iterations and decides retry vs. escalate. Phase handlers live in
  `coordinator/dev_phase.py`, `review_phase.py`, `validate_phase.py`.
- **REVIEW** is cross-model and structured: findings require
  `observed`/`expected`/`evidence`, APPROVE requires per-acceptance-criterion
  verification, and contract violations trigger corrective retries — see
  [Inputs Reference](guides/inputs-reference.md) and
  [ADR-0005](adr/0005-commit-centric-review-handoff.md) (reviewers evaluate
  commits and diffs, not pre-groomed file lists).

## The audit substrate

Every run writes durable records twice: a human-readable snapshot
(`.forge/audits/forge_audit.yaml`, per-run JSON under `.forge/audits/runs/`)
and the authoritative, queryable SQLite substrate at
`.forge/audits/index.sqlite`. Two properties matter
([ADR-0002](adr/0002-audit-substrate-and-queryable-run-history.md)):

1. **Refusal to forget.** There is no operator-facing delete/redact API for
   run records. Failures cannot be erased to make current state look better.
2. **It is the read path, not just a log.** Routing, escalation memory,
   `forge explain`, `forge audits`, and skip/staleness queries all read from
   the substrate. `assignment_history.yaml` is a derived view, not a source.

## The adaptive routing loop

With durable history (v0.11) and clean failure signals (v0.12), routing became
a learning loop (v0.13): complexity-score buckets pick tiers and reasoning
effort, escalation history and per-model profiles rerank candidates, domain
signal adjusts matches, and bounded exploration periodically re-tests benched
models so a bad early sample is not a life sentence.

The trust boundary
([ADR-0006](adr/0006-adaptive-router-trust-boundary.md)): only admissible,
untainted evidence carries routing weight, decisions must be explainable from
recorded evidence (`forge explain`), and taint governs routing weight only —
it does not censor knowledge or prompt context. Operator docs:
[Routing Policy](guides/routing-policy.md) and
[Adaptive Assignment](guides/adaptive-assignment.md).

## Module map

```text
src/theforge/
|- cli/            entry points and subcommands (run, sprint, status, audits, …)
|- config/         forge.yaml loading, auth, typed config, model registry
|- coordinator/    the deterministic state machine, phase handlers, audit writers
|- runners/        CLI and API runners + provider adapters (claude, codex, gemini, ghaw, api)
|- sprint/         manifest parsing, shape gate, DAG scheduling, sprint runner
|- intake/         issue-shape heuristics, diagnosis staleness
|- shape_check/    the typed intake verdict vocabulary
|- task/           prompt builders (dev, review, plan)
|- assignment.py   adaptive role assignment (eligibility, taint, recency, exploration)
|- routing.py      complexity buckets and reasoning-effort policy
|- admissibility.py issue admissibility (labels vs. live shape checks)
|- review.py / schemas.py   review parsing, normalization, schema validation
`- process_group.py         process-tree supervision for agent subprocesses
```

## Trust boundaries, in one place

| Boundary | Rule | Where |
|----------|------|-------|
| Control flow | Models never decide retry/gate/escalate | `coordinator/` (ADR-0002 preamble; README invariant) |
| Intake | Unready work is refused with a recorded reason, not guessed at | `shape_check/`, `admissibility.py` (ADR-0001, ADR-0003) |
| History | Run records are append-only; no delete/redact API | `coordinator/audit_substrate.py` (ADR-0002) |
| Routing | Only admissible, untainted evidence carries routing weight; decisions are explainable | `assignment.py`, `routing.py` (ADR-0006) |
| Execution | Agent edits happen in isolated worktrees; sandbox + verification broker for dev-run commands | `runners/sandbox.py`, `coordinator/dev_verification.py` (ADR-0004, ADR-0007) |

## ADR index

- [ADR-0001](adr/0001-intake-readiness-workflow.md) — intake readiness: the typed verdict vocabulary and the shape → diagnose → groom workflow.
- [ADR-0002](adr/0002-audit-substrate-and-queryable-run-history.md) — the audit substrate: SQLite as the authoritative, append-only run history and read path.
- [ADR-0003](adr/0003-intake-state-authority-and-label-reconciliation.md) — who is authoritative when labels and issue bodies disagree (carries an as-built divergence note).
- [ADR-0004](adr/0004-execution-substrate.md) — native vs. container execution: deferred, with re-entry conditions.
- [ADR-0005](adr/0005-commit-centric-review-handoff.md) — reviewers evaluate commits against spec; no file pre-grooming.
- [ADR-0006](adr/0006-adaptive-router-trust-boundary.md) — what evidence the adaptive router may learn from, and the explainability requirement.
- [ADR-0007](adr/0007-dev-phase-verification-capability.md) — the dev-phase verification broker: named, allowlisted commands only.
