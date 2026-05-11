# ADR-0002: Audit Substrate and Queryable Run History

- **Status:** Proposed
- **Date:** 2026-05-10 (proposed)
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** v0.11.x (substrate landing), v0.12+ (autonomy depends on these invariants), v0.13+ (adaptive router consumes the substrate)
- **Related issues:** #1465, #1467, #1470, #1324, #1509 / #1517, #1511, #1513, #1516
- **Related plans:** `docs/plans/forge-storage-layout.md` (file format, gitignore, migration), `docs/plans/knowledge-capture.md` (three-layer mechanism)

---

## Context

`docs/plans/forge-storage-layout.md` already specifies the *mechanism* for the v0.11 audit substrate: per-run JSON files under `.forge/audits/runs/`, a local SQLite index as the read path, `assignment_history.yaml` and `history.jsonl` reduced to derived views, default-deny gitignore with explicit re-includes, runtime preconditions, and a phased migration. Most of that work is already landing (#1324, #1465, #1467, #1470).

What the plan does not codify is the **trust model**. Several downstream decisions depend on knowing what the substrate is allowed to claim:

- **The intake-readiness slice** (ADR-0001) emits typed verdicts, grooming actions, diagnosis records, and inline-remediation events. Which of those become authoritative artifacts? Which are advisory?
- **The adaptive router** (v0.13+) will route on past performance. Which signals are it allowed to trust? Per-run records? LLM-generated summaries? Both?
- **Refusal-capability** (`project_north_star.md`) is TheForge's claimed core property. Making refusal *legible as data* — the remediation-to-runnable cost ratio that demonstrates upstream refusal saves downstream spend — requires the substrate to answer specific queries.
- **Compound engineering** (the doctrine of leverage-from-every-artifact) only works if the substrate guarantees evidence persists. A run that fails and leaves no trace is non-compounding by construction.

Without an ADR, each of these is decided ad-hoc inside the implementing PR. That is exactly the failure mode ADRs exist to prevent.

This ADR establishes the trust boundaries of the substrate so the next round of features (intake-readiness substrate emission, autonomous routing, adaptive learning) can cite them rather than re-deriving them per slice.

## Decision

### Headline principle

> **The audit substrate is the single source of truth for run history. Per-run records are authoritative and immutable. Derived views are queryable but not authoritative. LLM-generated summaries advise but never decide.**

### Trust boundaries

The substrate's contract is articulated in six clauses. Every downstream feature that touches run history must answer to one or more of these.

#### 1. What is authoritative

`.forge/audits/runs/{run_id}.json` — one file per run, tracked by default (per `forge init --shared-memory`).

A per-run record is the canonical, immutable description of what happened during that run. Specifically:

- `run_id` is globally unique and stable forever; never reused, never reassigned.
- `parent_run_id` links resume-style runs back to their predecessor for lineage.
- `schema_version` declares the record's shape. Readers MUST check this before parsing.
- `forge_version` declares the writer's version so mixed-version readers can skip records they don't understand.
- The record is written exactly once, at run termination, after a mandatory redaction pass (per `forge-storage-layout.md`).
- Mid-run state lives in `.forge/runs/` (machine-local, untracked). That is execution state, not audit. The two directories are distinct by design.

If a piece of data is not in a per-run record, it is not authoritative. Sprint summaries, status displays, intake remediation logs, and audit YAML pointers are all reflections of the per-run records, not parallel sources of truth.

#### 2. What is derived

Everything else under `.forge/audits/` is derived from the per-run records:

- `.forge/audits/index.sqlite` — local query index. Gitignored. Rebuildable on demand via `forge audits rebuild`. Zero merge surface.
- `.forge/audits/history.jsonl` — legacy compat during Phase A–C migration. Gitignored after Phase C. Will not exist at all in post-v0.11 fresh installs.
- `.forge/audits/forge_audit.yaml` — latest-run convenience pointer. Gitignored.
- `.forge/assignment_history.yaml` — derived view (post-migration). Gitignored.

A derived view that disagrees with the per-run records is a bug in the derivation, not a competing claim. The rebuild command exists specifically so derived state can be discarded and recomputed without operator anxiety.

#### 3. What is queryable

The SQLite index is the read path for all structured queries. Its schema MUST support, at minimum:

- `count / GROUP BY` over: `verdict`, `phase`, `outcome`, `model`, `slug`, `milestone`.
- Per-issue trajectory: `SELECT * WHERE issue_id = ? ORDER BY timestamp` (the shape needed to compute the readiness → ready transition rate per issue).
- Time-windowed aggregation: `WHERE started_at BETWEEN ? AND ?` (the shape needed for milestone-bounded postmortems).
- Cost aggregation: `SUM(cost) GROUP BY phase, milestone` (the shape needed for the refusal-economics metric below).

Indexed columns are stable across schema versions. New schema versions may add columns; they MUST NOT remove or repurpose existing ones — readers from older `forge_version` clients still need to query the substrate. (Adding columns is non-breaking; renaming or dropping is breaking and requires a `schema_version` bump.)

Staleness of the index is detected by file-presence cross-reference against `runs/*.json`, not by `mtime` alone. `forge audits rebuild` is the manual escape hatch.

#### 4. What the router may trust

The adaptive router (v0.13+) and the assignment policy (today, in degraded form) may make routing decisions on:

- Per-run records (cost, duration, outcome, iteration count, finding registry, model used, phase, slug).
- Aggregations over per-run records via the SQLite index.
- Schema-versioned fields that have shipped in at least one tagged release. New fields are not routing-trusted until they have a release floor under them.

The router MUST NOT route on:

- LLM-generated summaries (`.forge/knowledge/summaries/`). These are Layer 2 context inputs for future agent prompts, not mechanical decision inputs. (See clause 5.)
- Ad-hoc state files outside the substrate (e.g., the legacy `assignment_history.yaml` after Phase C).
- Records from `forge_version` newer than the reader. Skip with a warning; do not guess at unknown fields.

The router's trust surface is exactly the substrate. Anything else is advisory.

#### 5. What LLM summaries may and may not influence

`.forge/knowledge/summaries/{run_id}.yaml` (knowledge-capture Layer 2) are LLM-generated condensations of run history. They feed Layer 3 — the future-runs context-assembly machinery — so agents stop rediscovering the same conventions and failure modes.

Summaries are advisory. Specifically:

- **May influence:** prompt construction for plan, dev, and review phases. Context manifests that select which summaries are relevant for the current story. Operator-facing search and discovery.
- **MUST NOT influence:** routing decisions, budget enforcement, refusal verdicts, gate pass/fail outcomes, or any mechanical action the coordinator takes. Those decisions belong to the per-run records and the deterministic gates.

This is the *"LLMs propose, the coordinator decides"* invariant applied to history. Mechanical control flow stays on telemetry; LLM-generated text stays on context.

If a future feature wants to act on a pattern observed in summaries (e.g., "this kind of bug always escalates"), it must first record that pattern in the per-run substrate (e.g., as a structured tag on the runs the pattern applies to), and act on the structured signal — not on the prose summary.

#### 6. What intake-readiness commands must emit

Every intake-readiness command (`forge shape`, `forge groom`, `forge diagnose`, the shape-gate verdict emitter, and the inline-remediation path) writes a structured event into the substrate at the point it is currently emitting to YAML or stdout. Specific per-issue requirements live in the implementation issues (#1509/#1517, #1511, #1513, #1516); the substrate-level invariants are:

- Every emission carries `issue_id`, `command` (or gate name), `timestamp`, `base_sha`, and `run_id` (or `sprint_id`, if not run-scoped).
- Emissions are **observability, not gating.** If a substrate write fails, the originating command logs a WARNING and proceeds. The substrate's job is to be honest about what happened, not to be load-bearing on the happy path. (Gating is the job of the per-command refusal logic, which has its own guarantees.)
- Emissions are **additive to existing surfaces.** YAML logs, sprint summaries, and stdout output continue to emit as they do today; the substrate write is a parallel side effect, not a replacement.
- The schema for readiness events lives in a single section of the substrate (e.g., a dedicated table or a `kind` discriminator), so the refusal-economics metric is a single query, not a join across heterogeneous shapes.

### Refusal-to-forget invariant

Per-run records are immutable after write. The substrate provides no operator-facing API for deleting failure evidence in the normal workflow. Specifically:

- There is no `forge audits delete <run_id>` command.
- There is no `forge audits redact <run_id>` command. The redaction pass at write time is the only sanctioned modification; after that, the record stands.
- Operators who must remove a leaked secret from history use git-history rewriting tools (e.g., `git filter-repo`). This is intentionally inconvenient because it must remain a deliberate operator decision, not a casual cleanup.

This is the property that makes compound engineering possible: failures cannot be erased to make the current state look better. The audit substrate is a record, not a portfolio.

The rebuild command (`forge audits rebuild`) is exempt — it operates only on derived views (the SQLite index), never on the per-run records themselves. Rebuilds always reproduce the same data from the same source.

### Schema versioning is load-bearing

The substrate evolves over the project's lifetime. The contract that keeps that evolution safe:

- Every per-run record has `schema_version: int`.
- Every reader checks `schema_version` before parsing and routes to a migration helper if the version is below its known maximum.
- Adding a field is non-breaking and does not bump the version.
- Renaming, removing, or repurposing a field is breaking and MUST bump the version. Migration helpers are added in the same PR.
- The substrate must support reading records from at least the two most recent shipped `schema_version`s at any given time. (Older readers from outside the active support window may skip with a warning, per clause 4.)

No flag days. Schema migrations are reader-side, lazy, and per-record.

### Refusal-economics metric

The substrate must support computing — via a single query against the SQLite index — the **remediation-to-runnable cost ratio** per milestone:

```
ratio = (total_cost_of_inline_remediation_events + cost_of_runs_that_were_refused)
        / total_cost_of_runnable_runs
```

This metric makes refusal-capability legible as a number. Today, refusal feels like friction to operators because the cost avoided is invisible. Once the metric is queryable, postmortem and release-readiness reviews can cite the upstream-refusal savings directly.

The metric itself is not implemented by this ADR; the *substrate's obligation* to make it computable is. Specifically: the substrate must record cost, milestone, and outcome on every refused-at-gate event, every inline-remediation event, and every completed run. The metric is then a single aggregation query.

## Out of scope

Explicitly deferred to later ADRs or implementation issues:

- **Cross-repo / shared substrate.** Future work (`forge audits query --remote`). The SQLite column shapes should leave room for a `repo` dimension; population is deferred.
- **Retention policies, compaction, archival.** Real future concerns but not load-bearing for v0.11. Per-run files are tracked indefinitely.
- **AST-level staleness heuristics.** Issue #1516 covers content-hash staleness for diagnosis baselines; smarter heuristics belong to a later slice if dogfood proves them necessary.
- **A `forge audits delete` or `forge audits redact` command.** Explicitly excluded by the refusal-to-forget invariant.
- **Migration tooling for the v0.10 → v0.11 substrate transition.** Solo-operator memo: no migration ramp; existing repos either run `forge audits rebuild` once or live with the dual-write artifacts until they age out.
- **A schema for Layer 3 feedback** (how summaries feed prompts). Knowledge-capture's design covers the mechanism; the routing rules around what summaries are allowed to influence are codified by clause 5 above, but the prompt-construction details are implementation, not architecture.

## Consequences

### Positive

- Downstream features (router, autonomy, refusal economics) have a single document to cite. ADRs proliferate horizontally; they don't have to re-establish trust each time.
- The refusal-to-forget invariant makes compound engineering's "non-evaporation" property mechanically enforced, not aspirational.
- The clear separation between authoritative records and advisory LLM summaries forecloses an entire class of future mistakes (LLM-generated drift influencing mechanical decisions).
- Schema versioning + reader-side migration means the substrate can evolve without flag days.
- The substrate's query obligation makes the refusal-economics metric a one-query implementation rather than an architectural argument.

### Negative

- The "no delete, no redact" stance is intentionally inconvenient. Operators who leak a secret into an audit record have to do git-history rewriting to remove it. This is by design but is a real operator cost.
- Substrate emission obligations expand the per-PR test surface for every intake-readiness slice. (Mitigated: the test shape is uniform across slices once #1517 lands.)
- Mixed-version reading (clause 4) requires every reader to handle "I don't understand this `schema_version`" gracefully. This is small but pervasive.

### Risks

- **Substrate write becomes load-bearing despite clause 6.** A future contributor accidentally treats substrate-write success as a gate prerequisite. Mitigated by uniform "log and proceed" pattern in the shared writer and a single seam-level test that covers it.
- **LLM summaries are used as decision inputs anyway.** Clause 5 forbids it, but routing or scoring code could grow a covert dependency on summary content via prompt manipulation. Mitigated by keeping summaries off the router's read path entirely — the router consumes the SQLite index, not `.forge/knowledge/`.
- **Schema drift goes unnoticed.** A field is renamed without a version bump and old readers silently break. Mitigated by a single linter (added as a follow-up issue) that requires `schema_version` change when the per-run record dataclass changes.
- **The refusal-economics metric becomes vibes.** The query exists but no postmortem cites it, so refusal stays "feels like friction." Mitigated by surfacing it in `forge audits show` (already in flight via #1470) and in release-readiness reviews going forward.

## References

- `docs/plans/forge-storage-layout.md` — file format, gitignore, migration sequence, runtime preconditions
- `docs/plans/knowledge-capture.md` — three-layer capture/summarize/feed-forward mechanism
- ADR-0001 — intake readiness workflow (emits into this substrate)
- #1324, #1465, #1467, #1470 — substrate implementation slices already landing
- #1509 / #1517 — shape-gate verdict emission
- #1511, #1513, #1516 — intake-readiness substrate emission obligations
- `project_north_star.md` (memory) — refusal-capability as TheForge's core property; this ADR makes the substrate side of that property concrete
- `project_full_audit_trail.md` (memory) — durable full-output capture intent; promoted from memory to ADR
