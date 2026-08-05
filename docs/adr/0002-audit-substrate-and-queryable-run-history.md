# ADR-0002: Audit Substrate and Queryable Run History

- **Status:** Proposed
- **Date:** 2026-05-10 (proposed)
- **Deciders:** Peter Wickersham (project lead), with iterative review by Claude and Codex
- **Affected milestones:** v0.11.x (substrate landing), v0.12+ (autonomy depends on these invariants), v0.13+ (adaptive router consumes the substrate)
- **Related issues:** #1465, #1467, #1470, #1324, #1509 / #1517, #1511, #1513, #1516, #1522 (substrate-schema obligations follow-up)
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

**Audit records with `provenance='native'` in the substrate.** Native provenance has two shapes today:

- **Per-run record files.** `.forge/audits/runs/{run_id}.json` — one file per run, tracked by default (per `forge init --shared-memory`). These are the canonical, immutable description of what happened during that run.
- **Programmatic native inserts.** Sprint rollups and other coordinator-level audit dicts written directly into the substrate without a per-run file source path. `audit_substrate.py` already treats these as native (the source-path-less branch in `_native_rows_are_stale`). They are equally authoritative.

The distinction between file-backed and file-less native rows is implementation, not architecture. **Provenance is the authority signal**, not file presence. Specifically:

- `run_id` is globally unique and stable forever; never reused, never reassigned.
- `parent_run_id` (where present) links resume-style runs back to their predecessor for lineage.
- File-backed native records additionally carry the per-file invariants from `forge-storage-layout.md`: written exactly once at run termination, after a mandatory redaction pass, immutable on disk.
- File-less native records (sprint rollups, etc.) are inserted programmatically by coordinator-owned writers; they obey the same upsert-and-stand contract via the substrate's `upsert_run_record` path with the native-row protection rule.
- Records with `provenance='legacy_history_jsonl'` are migration-compat only; they are not authoritative and any conflict with a native row leaves the native row in place. (See the native-row protection branch in `upsert_run_record`, which refuses a legacy-provenance import from overwriting an existing native row.)
- Mid-run state lives in `.forge/runs/` (machine-local, untracked). That is execution state, not audit. The two directories are distinct by design.

If a piece of data is not in a native-provenance substrate row, it is not authoritative. Sprint summaries, status displays, intake remediation logs, and audit YAML pointers are reflections of native rows, not parallel sources of truth.

The file-backed and file-less native shapes are both authoritative today. Whether they eventually unify under a single per-run JSON convention is an open implementation question, not an architectural commitment of this ADR.

#### 2. What is derived

Everything else under `.forge/audits/` is derived from the authoritative native-provenance records:

- `.forge/audits/index.sqlite` — local query index. Gitignored. Rebuildable on demand via `forge audits rebuild`. Zero merge surface.
- `.forge/audits/history.jsonl` — legacy compat during Phase A–C migration. Gitignored after Phase C. Will not exist at all in post-v0.11 fresh installs.
- `.forge/audits/forge_audit.yaml` — latest-run convenience pointer. Gitignored.
- `.forge/assignment_history.yaml` — derived view (post-migration). Gitignored.

A derived view that disagrees with the per-run records is a bug in the derivation, not a competing claim. The rebuild command exists specifically so derived state can be discarded and recomputed without operator anxiety.

#### 3. What is queryable

The SQLite index is the read path for all structured queries. The current schema indexes `slug` and `started_at` and exposes flat columns for `run_id`, `total_cost_usd`, `final_phase`, `outcome_success`, `branch`, `landing_status`, `complexity_score`, `provenance`, `source_path`, plus per-cycle `verdict` in the `reviews` table. The `raw_json` blob carries everything else.

This ADR establishes the **substrate's queryability obligation**, not the current column set. The substrate MUST eventually support, at minimum:

- `count / GROUP BY` over: `verdict` (at record level, not only per-cycle), `phase`, `outcome`, `dev_model`, `slug`, `milestone`.
- Per-issue trajectory: `SELECT * WHERE issue_id = ? ORDER BY timestamp` (the shape needed to compute the readiness → ready transition rate per issue).
- Time-windowed aggregation: `WHERE started_at BETWEEN ? AND ?` (already supported via the `idx_audit_records_started_at` index).
- Cost aggregation: `SUM(cost) GROUP BY phase, milestone` (the shape needed for the refusal-economics metric below).

The dimensions not yet indexed at the record level (`milestone`, `issue_id`, `dev_model`, record-level `verdict`, non-terminal `phase`) are tracked as substrate-schema obligations in **#1522**. Until those columns land, consumers parse the `raw_json` blob — correct but unscalable. ADR-0002 commits the substrate to closing that gap; it does not claim those columns ship today.

Indexed columns are stable once introduced. New schema versions may add columns; they MUST NOT remove or repurpose existing ones — readers from older `forge_version` clients still need to query the substrate. (Adding columns is non-breaking; renaming or dropping is breaking and requires a `schema_version` bump.)

Staleness of the index is detected by file-presence cross-reference against `runs/*.json`, not by `mtime` alone. `forge audits rebuild` is the manual escape hatch.

#### 4. What the router may trust

The adaptive router (v0.13+) and the assignment policy (today, in degraded form) may make routing decisions on:

- Per-run records (cost, duration, outcome, iteration count, finding registry, model used, phase, slug).
- Aggregations over per-run records via the SQLite index.
- Schema-versioned fields that have shipped in at least one tagged release. New fields are not routing-trusted until they have a release floor under them.

The router MUST NOT route on:

- LLM-generated summaries (`.forge/knowledge/summaries/`). These are Layer 2 context inputs for future agent prompts, not mechanical decision inputs. (See clause 5.)
- Ad-hoc state files outside the substrate (e.g., the legacy `assignment_history.yaml` after Phase C).
- Records from `forge_version` newer than the reader. Skip with a warning; do not guess at unknown fields. *(Today `forge_version` is recorded per record but hardcoded to a placeholder value; meaningful per-release population is part of the per-record schema versioning work in #1522. The clause states the forward-looking contract that the reader-side dispatch in #1522 will rely on.)*

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

The substrate evolves over the project's lifetime. The contract that keeps that evolution safe has two layers, one shipped and one obligated:

**Shipped today:**

- A substrate-level schema version (`SUBSTRATE_SCHEMA_VERSION = 2`) lives in the `meta` table of the SQLite index. `_apply_schema` is idempotent and reapplies on every open, so the index file can evolve forward without explicit migrations.
- The substrate refuses corrupt or unreadable files and points at `forge audits rebuild` (see `SubstrateCorruptError`).

**Obligated (tracked in #1522):**

- Every per-run record gains a top-level `schema_version: int` field. Records written before this slice are treated as `schema_version=1`.
- Readers consult per-record `schema_version` before parsing and route through a migration helper when the version is below their known maximum.
- Adding a field is non-breaking and does not bump the version. Renaming, removing, or repurposing a field is breaking and MUST bump the version, with migration helpers added in the same PR.
- The substrate must support reading records from at least the two most recent shipped `schema_version`s at any given time. (Older readers from outside the active support window may skip with a warning, per clause 4.)

**Landed in #1522 (substrate-side reader dispatch):**

- `audit_records` gains indexed columns `record_schema_version`, `milestone`, `issue_id`, `dev_model`, and `verdict` (run-level, derived from the final review cycle), plus indexes on `final_phase` and `outcome_success`, satisfying the minimum-query dimensions named in clause 3. The new `verdict` column is distinct from `reviews.verdict` (per-cycle) so `COUNT/GROUP BY` queries at record granularity no longer require joining `reviews`. Missing source fields populate as NULL; existing rows pick up the new columns on the next `forge audits rebuild`.
- New per-run records are written at `schema_version=2` (`CURRENT_RECORD_SCHEMA_VERSION` in `audit_substrate.py`). Pre-slice records without a `schema_version` field are read as version 1.
- `audit_substrate._migrate_record(record, from_version=...)` is the reader-side seam. It is a no-op today (no breaking field changes have shipped) but every reader (`iter_records`, `tail_records`, `iter_escalation_records`, `has_review_approve_in_substrate`) now consults the indexed `record_schema_version` and routes the parsed record through it. Future breaking changes register translations there.

**Landed in #2225 (one identity namespace for `dev_model`):**

- `dev_model` had indexed whatever spelling the runner recorded (`anthropic/sonnet/cli`, `sonnet`, `claude-sonnet-4-6`), so `GROUP BY dev_model` — a clause-3 query dimension — returned several partial populations for one model. Identities are now canonicalized at the shared `coordinator.agent_identity` projection, using the `transport_used` the renderer records to disambiguate bare model names the catalog offers over both transports.
- A spelling that still cannot be resolved is kept verbatim, which is the truthful record, but `audit_records.dev_model_resolution` now says whether the stored value is `canonical` or `unresolved`, so a consumer can tell an unrecognized identity from a normalized one instead of treating both as equally authoritative.
- `SUBSTRATE_SCHEMA_VERSION` is 6. Opening a version-5 substrate re-derives the `dev_model*` columns from each row's `raw_json`, so the normalization reaches already-indexed history rather than only new writes.

**Writer-side guard (tracked in #1528):**

- A CI check refuses a `schema_version` bump on the writer side without a matching migration-helper entry. This is the writer-side counterpart to #1522's reader-side dispatch; the two issues are sized to land independently.

No flag days. Schema migrations are reader-side, lazy, and per-record. This ADR commits to that property; #1522 + #1528 implement it together.

### Refusal-economics metric

The substrate must support queries that distinguish three quantities, per milestone:

- **Cost spent on inline remediation events** — what we paid because upstream grooming was skipped at intake (`intake.grooming: true` fired at sprint entry per ADR-0001's posture).
- **Cost spent on runs that proceeded after upstream grooming** — the "clean intake" path's cost footprint.
- **Counts of refusals at each gate** — how often each gate declined to proceed, with verdict identifier and base SHA.

From these three dimensions, postmortem and release-readiness reviews can derive whatever ratio or trend makes refusal-capability legible — e.g., remediation cost as a fraction of clean-intake cost, or refusal rate over time. The exact formula belongs in the dogfood / postmortem slice that consumes these dimensions, not in this ADR. **The ADR's obligation is to make those three dimensions queryable**, not to fix the metric shape.

This matters because refusal feels like friction to operators today — the cost avoided is invisible. The dimensions above make the cost avoided countable, even if the headline metric's formula evolves with use.

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
- The substrate's query obligations make refusal-economics inputs (inline-remediation cost, clean-intake-run cost, refusal counts) measurable and queryable rather than the subject of an architectural argument. The headline formula is then a downstream choice, not an ADR-bound contract.

### Negative

- The "no delete, no redact" stance is intentionally inconvenient. Operators who leak a secret into an audit record have to do git-history rewriting to remove it. This is by design but is a real operator cost.
- Substrate emission obligations expand the per-PR test surface for every intake-readiness slice. (Mitigated: the test shape is uniform across slices once #1517 lands.)
- Mixed-version reading (clause 4) requires every reader to handle "I don't understand this `schema_version`" gracefully. This is small but pervasive.

### Risks

- **Substrate write becomes load-bearing despite clause 6.** A future contributor accidentally treats substrate-write success as a gate prerequisite. Mitigated by uniform "log and proceed" pattern in the shared writer and a single seam-level test that covers it.
- **LLM summaries are used as decision inputs anyway.** Clause 5 forbids it, but routing or scoring code could grow a covert dependency on summary content via prompt manipulation. Mitigated by keeping summaries off the router's read path entirely — the router consumes the SQLite index, not `.forge/knowledge/`.
- **Schema drift goes unnoticed.** A field is renamed without a version bump and old readers silently break. Mitigated by the CI guard tracked in #1528, which fails when the per-run record's serializable shape changes without a corresponding `schema_version` bump and migration helper entry.
- **The refusal-economics dimensions exist but go unused.** The substrate surfaces inline-remediation cost, clean-intake-run cost, and refusal counts, but if no postmortem queries them, refusal stays "feels like friction." Mitigated by surfacing the dimensions in `forge audits show` (already in flight via #1470) and citing them in release-readiness reviews going forward.

## References

- `docs/plans/forge-storage-layout.md` — file format, gitignore, migration sequence, runtime preconditions
- `docs/plans/knowledge-capture.md` — three-layer capture/summarize/feed-forward mechanism
- ADR-0001 — intake readiness workflow (emits into this substrate)
- #1324, #1465, #1467, #1470 — substrate implementation slices already landing
- #1509 / #1517 — shape-gate verdict emission
- #1511, #1513, #1516 — intake-readiness substrate emission obligations
- `project_north_star.md` (memory) — refusal-capability as TheForge's core property; this ADR makes the substrate side of that property concrete
- `project_full_audit_trail.md` (memory) — durable full-output capture intent and `forge.yaml audit.level` configurability. **Related thread; not superseded by this ADR.** The memory entry's two concerns (storing full prompt/response content in audit, and operator-configurable audit verbosity) sit outside this ADR's trust-boundary scope. Full-output capture continues to be governed by `docs/plans/knowledge-capture.md` (Layer 1 surfaces a subset; raw prompts and full responses remain in `.forge/logs/`); audit-level configurability is open future work and would warrant its own ADR if/when it ships.
- #1522 — substrate-schema obligations (per-record schema versioning, indexed dimensions for routing and refusal-economics)
