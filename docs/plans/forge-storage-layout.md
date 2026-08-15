# Design Note: `.forge/` Storage Layout & Git Policy

**Status: Shipped (v0.11.0).** The audit substrate
(`coordinator/audit_substrate.py`, `forge audits rebuild`) and the git policy
described here are live. Predecessor to knowledge-capture Layer 1. Settles what
gets tracked, what format it takes, and how existing consumers migrate.

---

## The Decision

**Shared project memory by default, merge-friendly by construction,
local-only by init-time flag.**

TheForge's audit and knowledge artifacts are part of the project's working
memory — the same trust boundary as the source code. They should travel with
the repo by default. Teams with stricter data boundaries opt in to local-only
mode when they run `forge init`, not via a runtime config that changes
behavior partway through a project's life.

The default is not "memory is dangerous, uncomment if you dare." It is
"project memory is designed to be shared. If your repo is public or you have
stricter data boundaries, pass `--local-memory` to `forge init` and we'll
write the template that keeps everything on your machine."

**On the OSS concern:** Public-source projects are the one scenario where
shared memory by default is genuinely the wrong call — audit records can
contain exploratory prompts, partial plans, or cost data that maintainers
don't want in the public diff noise. The answer is not a runtime mode but two
concrete tools: (1) `forge init --local-memory` writes a template that
gitignores `runs/` and `summaries/` from the start; (2) the `.gitattributes`
generation below marks tracked per-run files as `linguist-generated=true`,
which reduces PR diff noise for projects that do track them (mitigation,
not a guarantee — see the `.gitattributes` section for specifics).
Together these cover the OSS case without muddying the default.

---

## Design Principles

Two principles govern every choice in this doc. They are stated here so later
sections can be read against them.

### 1. Shared memory, local override at init

Project memory travels with the repo by default. Opt-out happens once, at
`forge init`, not as a runtime mode that can be flipped mid-project. A
runtime mode invites "it worked on my machine" class bugs where two
contributors disagree about whether a record exists.

### 2. PR-conflict minimization is load-bearing

> Choices should keep PR conflicts minimal unless the alternative can be
> considered significant complexity.

Complexity is paid once by the implementer. Conflicts are paid forever by
every user on every parallel run. When the two trade off, accept complexity
up to a meaningful threshold before accepting conflict surface.

This principle has direct consequences:

- Append-only shared logs are banned. `history.jsonl` is merge-conflict bait
  by construction and must become a derived view.
- Single-file rewritten-on-every-run state is banned. `assignment_history.yaml`
  in its current shape violates this and must become a derived view too (see
  "Assignment History" below).
- Per-run files are preferred because parallel runs on different branches
  produce differently-named files, and git merges them trivially.
- Binary indexes (SQLite) are acceptable on the read path because they are
  local-only and gitignored — they have zero conflict surface by definition.

---

## The Category Model

Four categories, enforced by both `.gitignore` and runtime checks:

| Category | Examples | Tracked? | Committing would... |
|----------|----------|----------|---------------------|
| **Secrets** | `.env`, `secrets.yaml` | Never | Leak credentials |
| **Machine-local runtime** | `worktrees/`, `locks/`, `merge.lock`, `daemon.json`, `pending/`, `runs/` (the execution state dir, not the audit dir), root `handoff.yaml` | Never | Break runs: stale PIDs, nested git repos, cross-machine lock false-positives |
| **Noise / derived views** | `logs/`, `index/`, `audits/history.jsonl`, `audits/index.sqlite`, `knowledge/index.yaml`, `knowledge/invariants/index.yaml`, `assignment_history.yaml` (derived) | Never (derived or ephemeral) | Create churn or merge conflicts; regenerable |
| **Project memory + config** | `hooks/`, `.env.example`, `audits/runs/{run_id}.json`, `knowledge/summaries/{run_id}.yaml` | **Default on** (opt out via `forge init --local-memory`) | Nothing — this is the working memory of the project |

The third category is important: **derived views are never tracked, even
though they contain tracked data.** `history.jsonl`, `index.sqlite`,
`knowledge/index.yaml`, `knowledge/invariants/index.yaml`, and
`assignment_history.yaml` are rebuildable from per-run files or, for the
invariant index (#1875), from the marked Markdown sources it derives from. Tracking them would create merge-conflict bait without adding
information the per-run files don't already have.

### Naming collision note

There are two `runs/` directories with unfortunately similar names:

- `.forge/runs/` — machine-local **execution state** (active run artifacts,
  temp files, worktree bookkeeping). Never tracked.
- `.forge/audits/runs/` — the canonical **per-run audit records**. Tracked by
  default.

Keep them straight. The migration story below is about the audit dir, not
the execution dir.

---

## The Format Change

The single most important decision: **audit records become per-run files,
not append-only logs.** The canonical read path becomes a local SQLite index,
rebuildable on demand from the per-run files.

### Today

```
.forge/audits/forge_audit.yaml      # overwritten every run (useless as history)
.forge/audits/history.jsonl         # append-only, single-writer, merge-conflict bait
```

### Target

```
.forge/audits/runs/{run_id}.json    # canonical per-run record — tracked, immutable
.forge/audits/index.sqlite          # local read-path index — ignored, auto-rebuilt
.forge/audits/history.jsonl         # migration compat only, dropped in Phase D — ignored
.forge/audits/forge_audit.yaml      # latest-run convenience pointer — ignored
```

Per-run files merge the way source files do: parallel runs on different
branches produce differently-named files, git takes both, done. The rollup
becomes a locally-materialized binary index, rebuilt on demand from `runs/`.

`run_id` is already generated in `run_setup.py` (line 218) and passed to
`StructuredLogger`. Layer 1 of knowledge-capture surfaces it into the audit
record. It becomes the stable filename.

### Per-run file contract

Every file in `.forge/audits/runs/` obeys the following invariants:

- **`run_id` is globally unique.** Generated in `run_setup.py`; never reused.
- **Immutable once written.** A run record is written exactly once, when the
  run terminates. Mid-run updates go to execution state under `.forge/runs/`,
  not here. This is what makes per-run files safe to track: they never change
  after commit, so rebases and cherry-picks are trivial.
- **`parent_run_id: string | null`** links resume-style runs back to their
  predecessor. This preserves lineage for tasks that span multiple runs
  (timeouts, human-interrupted retries, future story decomposition in
  agent-intelligence Phase 11 — each sub-task is its own run with a pointer
  back to the parent).
- **`schema_version: int`** at the top level. Bumped on every breaking change
  to the record shape. Readers check this before parsing and fall back to
  migration helpers for old versions. This is how we avoid a flag day when
  the audit schema evolves.
- **Best-effort redaction pass before write.** A mandatory redaction pass
  runs before serializing. It is best-effort, not a proof of absence —
  arbitrary story text or plan content can still embed secrets the writer
  can't recognize. What the pass does do, recursively over the audit object:
  (1) load `.forge/.env` if present, parse `KEY=VALUE` lines (ignore
  comments, skip empty values, skip values shorter than 8 chars to avoid
  replacing common strings like `true` or `dev`), and scrub any occurrence
  of a matched value anywhere in the record; (2) scrub values of any key
  matching `(?i)(secret|token|password|api[_-]?key|authorization)`; (3)
  replace `runtime.environment` with a key-only listing. Redaction is
  applied in the writer, not the reader, so the file on disk is what ships
  in the repo. Operators who need stronger guarantees layer their own
  pre-commit hooks on top.

### SQLite index as the read path

`history.jsonl` was doing double duty: it was both the persistent log and the
query index. Splitting those jobs:

- **Persistent log → per-run JSON files.** Source of truth, tracked,
  mergeable.
- **Query index → `.forge/audits/index.sqlite`.** Local, binary, gitignored.
  Zero merge-conflict surface (it's a binary file nobody commits). Contains
  indexed columns for the common query shapes: `run_id`, `slug`, `phase`,
  `outcome`, `model`, `started_at`, plus a full-record blob column.
- **Staleness detection.** The correctness check is file-presence: on open,
  the reader lists `runs/*.json` and cross-references against `run_id`s
  recorded in the index. Any file missing from the index, or any indexed
  `run_id` whose file is gone, triggers a rebuild. `mtime` is retained as
  a fast-path optimization (compare max `mtime` against the index's
  `built_at` to skip the presence check on the common no-change path), but
  `mtime` alone is not load-bearing — checkout, merge, and clone operations
  can rewrite timestamps in ways that would otherwise fool it. Rebuilds
  stream the `runs/` directory in sorted order; the target is "fast
  enough for interactive use" — measure on dogfood data before committing
  to a harder number.
- **`forge audits rebuild`** is the manual escape hatch: deletes the index
  and rebuilds from scratch.

`sqlite3` is Python stdlib, so this is not a new dependency. The index gives
us fast structured queries (telemetry aggregation, sprint rollup,
`_prior_approve_recorded`) without parsing JSONL from scratch on every call.

### Why per-run JSON and not YAML

The existing rollup is JSONL — line-delimited JSON. Per-run files should be
JSON too for format consistency and faster loading. Human-readability is
already served by `forge_audit.yaml` (the latest-run convenience copy,
which stays YAML).

---

## Assignment History

`.forge/assignment_history.yaml` today is a single YAML file rewritten on
every run to record which models were assigned to which slugs. It violates
PR-conflict minimization directly: every parallel run on a different branch
rewrites the same file, and every merge hits the same conflict.

The information it holds is already in the per-run audit records
(`runtime.assignments`, plus phase-by-phase model selection). The fix is to
stop treating `assignment_history.yaml` as tracked state and start treating
it as a derived view over `audits/runs/`:

- **Writer side:** delete the direct writes. The per-run audit record already
  captures the assignment at run time.
- **Reader side:** the callers that today load `assignment_history.yaml` to
  make adaptive-assignment decisions query the SQLite index instead (or the
  JSON files directly during Phase B).
- **File status:** moved to the "noise / derived views" category. Gitignored.
  A thin rebuild command (`forge audits rebuild` or a sibling) regenerates
  a YAML snapshot locally for operators who want to eyeball it.

This is a breaking change to the adaptive-assignment code path. It is
staged into the migration sequence below alongside the other consumers.

---

## Consumers of `history.jsonl` — Migration Path

A ripgrep inventory turned up five functional consumers plus tests, plus the
assignment-history writer/reader pair:

| Consumer | Purpose | Migration |
|----------|---------|-----------|
| `coordinator/audit.py` `_prior_approve_recorded()` | Skip-review eligibility when re-running | Query SQLite index by slug + filter for APPROVE |
| `sprint/audit.py` (2 call sites) | Sprint-level append + rollup | Write per-run files; sprint rollup queries the index |
| `cli/telemetry.py` — `forge telemetry` | Per-phase cost/duration aggregation | Query SQLite index directly |
| `cli/shared.py` `_append_history` | The writer | Replace with per-run file writer; redaction pass; retain JSONL append under a feature flag for Phases A–C |
| `assignment_history.yaml` writer/reader | Adaptive assignment state | Delete writer; readers query index |
| Tests | Fixture creation | Update to write per-run files (mechanical) |

### Migration sequence (no flag day)

1. **Phase A — Dual-write.** `_write_audit` writes
   `.forge/audits/runs/{run_id}.json` (with redaction pass, `schema_version`,
   `parent_run_id`) *and* continues appending to `history.jsonl`. Both
   consumers work unchanged. Tests verify both paths populate and that the
   redaction pass strips secrets.

2. **Phase B — Read from the index.** Stand up
   `.forge/audits/index.sqlite` with staleness detection and cold-rebuild.
   One consumer at a time, switch from scanning `history.jsonl` to querying
   the index. Order: telemetry → sprint rollup → `_prior_approve_recorded`
   → `assignment_history` readers. The last two are the highest-stakes —
   they gate review skipping and model selection.

   **Mixed-version fallback.** During Phase B, older clients in the same
   repo still read `history.jsonl`. New-version readers must rebuild the
   SQLite index from `runs/*.json` whenever they detect it missing or
   stale — readers never trust a partial index; rebuild-on-miss is cheap.
   While the dual-write is still active, the rebuild also imports any
   legacy-only records from `history.jsonl` that have no corresponding
   `runs/{run_id}.json` file, so readers get one unified view across the
   migration. A `forge_version` field in each run record lets readers
   detect and skip records written by newer clients they don't yet
   understand.

3. **Phase C — Stop writing `history.jsonl`.** Drop the dual-write.
   **Prerequisite:** at least one release has shipped where all known
   readers go through the audit repository / index abstraction, and the
   legacy-import rebuild path has seen real use. Tracked template for new
   projects already excludes `history.jsonl`. Existing projects keep their
   file as a historical artifact; by this point nothing reads it.
   `.forge/assignment_history.yaml` deletion lands in the same story, since
   both are now derived-only.

4. **Phase D — Clean up.** Delete the migration fallback code from readers.
   `history.jsonl` is gone from new installs; old installs can delete it
   manually or ignore it.

Phases A and B can land in a single story. Phases C and D are follow-ups once
the tracked-per-run path is proven on at least one real sprint.

---

## The Canonical `.gitignore` Block

This is what `forge init` writes. A single commented block, one source of
truth, embedded in TheForge so docs and runtime stay in sync.

**Default-deny with explicit re-includes.** A blanket `.forge/` ignore
followed by re-includes is the safer shape: when a future TheForge version
introduces a new directory under `.forge/`, it defaults to ignored rather
than accidentally-tracked. The prior draft used explicit-deny because
re-include syntax is fiddly, but the failure mode (a new runtime dir leaks
into repos on upgrade) is worse than the syntax cost (well-commented
re-includes are fine).

```gitignore
# ── TheForge ──────────────────────────────────────────────────────────
# TheForge writes everything under .forge/. We deny the whole directory
# by default and explicitly re-include the paths that are project memory
# (tracked, travel with the repo) and config (tracked, travel with the
# repo). Everything else — secrets, runtime state, derived views — stays
# local to your machine.
#
# To opt out of shared memory entirely (public repo, stricter data
# boundary), run `forge init --local-memory` or delete the project-memory
# re-includes below (the `audits/` and `knowledge/summaries/` lines —
# keep the hooks and .env.example re-includes, those are config).
#
# See: docs/guides/forge-storage.md

# Deny everything under .forge/ by default
.forge/**

# Re-include project memory (canonical per-run records)
!.forge/audits/
!.forge/audits/runs/
!.forge/audits/runs/**
!.forge/knowledge/
!.forge/knowledge/summaries/
!.forge/knowledge/summaries/**

# Re-include config
!.forge/hooks/
!.forge/hooks/**
!.forge/.env.example

# Root-level runtime state — always ignored
handoff.yaml
# ──────────────────────────────────────────────────────────────────────
```

**Why this shape:**

- **Default-deny fails closed for future directories.** If TheForge ships
  a new `.forge/cache/` or `.forge/scratch/` in a future version, existing
  users who pulled the template won't accidentally start tracking it.
- **Re-includes are explicit and auditable.** Anyone reading the template
  can see exactly what travels with the repo.
- **Opt-out mode (`forge init --local-memory`) just deletes the
  re-include lines.** The deny rule stays; the shared-memory exceptions
  go away.
- **`handoff.yaml` at the repo root is always ignored.** It's phase-routing
  state that gets overwritten every run. Cross-machine staleness causes
  wrong gate decisions.

### `.gitattributes` for tracked per-run files

`forge init` also writes (or appends to) `.gitattributes`:

```gitattributes
# ── TheForge ──────────────────────────────────────────────────────────
# Audit runs and knowledge summaries are generated by TheForge. Collapse
# them in PR diffs so reviewers focus on source changes.
.forge/audits/runs/**  linguist-generated=true
.forge/knowledge/summaries/**  linguist-generated=true
# ──────────────────────────────────────────────────────────────────────
```

This reduces PR diff noise for projects that track memory: GitHub
collapses the files in PR review by default and excludes them from
language-statistics churn. It is a mitigation, not a guarantee — file
lists, commit lists, and search results still surface them. Projects
that want stronger isolation use `--local-memory` instead.

### `forge init` flags

```
forge init [--shared-memory | --local-memory]
```

- `--shared-memory` (default): writes the template above as-is.
- `--local-memory`: writes the template with the `!.forge/audits/runs/` and
  `!.forge/knowledge/summaries/` re-includes omitted. Everything stays
  local. `.gitattributes` is still written (harmless if nothing is tracked).

The flag is resolved once at init time. Switching later means editing the
`.gitignore` by hand — intentional, since flipping mid-project would strand
already-committed records.

---

## Runtime Guards: `forge run` Preconditions

`forge init` writes the template, but some users will have pre-existing
repos or will have customized the template in ways that break. `forge run`
should catch the catastrophic cases before they matter.

**Blockers** (fail fast, don't start the run):

- `.forge/worktrees/**` is tracked
- `.forge/locks/**` is tracked
- `.forge/merge.lock` is tracked
- `.forge/daemon.json` is tracked
- `.forge/pending/**` is tracked (execution state)
- `.forge/runs/**` is tracked (execution state dir, not the audit dir)
- `.forge/.env` or `.forge/secrets.yaml` is tracked
- root `handoff.yaml` is tracked

**Warnings** (proceed but surface):

- `.forge/logs/**` is tracked (noise, not broken)
- `.forge/audits/history.jsonl` is tracked after Phase C migration (should
  be derived-local by then)
- `.forge/audits/index.sqlite` is tracked (binary churn; unintended)
- `.forge/assignment_history.yaml` is tracked after the derived-view
  migration

The existing `cli/run.py:59` already warns on tracked `secrets.yaml`. This
story extends that check to the full blocker/warning list. Messages must be
actionable: `".forge/worktrees/ is tracked in git — run 'git rm --cached -r
.forge/worktrees/' and commit"`.

---

## Scope Boundaries

**In scope for this design:**

- Category model and naming
- Per-run audit file format, naming, and contract (immutability,
  `parent_run_id`, `schema_version`, redaction pass)
- Canonical `.gitignore` template (default-deny shape)
- `.gitattributes` generation for `linguist-generated`
- `forge init --shared-memory | --local-memory`
- SQLite index as the read path
- Migration path for `history.jsonl` consumers with mixed-version fallback
- `assignment_history.yaml` migration to derived view
- Runtime precondition checks

**Out of scope (explicit non-goals):**

- Runtime config mode (`memory.mode: shared | local`). Rejected. Init-time
  flag is simpler and avoids mid-project flips.
- Moving learned state to a sibling directory (`.forge-history/`). Rejected
  for now — touches every path in audit/router/knowledge code with no
  proportional benefit once gitignore is crisp.
- Retention policies, compaction, archival. Real future concerns but not
  needed to ship the category model.
- Audit record schema changes beyond adding `run_id`, `parent_run_id`,
  `schema_version`, and the redaction pass (which Layer 1 leans on).
- Cross-repo/remote-indexed query (`forge audits query --remote`). Future
  work — the local index is the floor.

---

## Questions for Future Work

These came up in review but don't block this design. Flagging them so they
don't get forgotten.

- **Feature branches vs main for audit commits.** Should per-run audit
  records be committed on the feature branch as part of the work, or
  squashed into the merge commit, or accumulated on a parallel
  `audits/` branch? Each has tradeoffs (lineage clarity vs PR noise vs
  tooling complexity). This doc assumes the first option (records commit
  alongside source on the feature branch) because it's the simplest and
  most git-native, but the question is open.

- **Large-repo scaling.** At what point does `runs/*.json` stop being
  cheap to list? 10k files is fine; 100k is a directory full of pain on
  some filesystems. Possible future move: shard by date
  (`runs/2026-04/{run_id}.json`). Not doing this yet because it changes
  the `run_id → path` mapping and complicates every reader.

- **Cross-repo query.** A future `forge audits query --repos 'foo,bar'`
  would need a shared index format. Out of scope here but worth keeping
  in mind when choosing SQLite column shapes — leave room for a `repo`
  dimension even if we don't populate it yet.

- **Audit record size.** Layer 1 of knowledge-capture adds story text,
  plan content, and plan regeneration lineage. A large run record could
  push into the tens of KB. Worth measuring on a real sprint before
  deciding whether compression or reference-to-worktree-blob is needed.

- **Public-repo review of tracked records.** Before dogfooding on this
  repo, spot-check a handful of `runs/*.json` to confirm the redaction
  pass catches what it needs to catch. If it doesn't, extend the pass
  before flipping the default.

---

## Story Sequence

This design decomposes into a story chain with three distinct classes of
dependency. Mixing them up is how migrations get scary for no reason, so
they're called out explicitly:

- **Implementation dependency: Story 1 before knowledge-capture Layer 1.**
  Layer 1 writes new fields into the per-run audit record. Those fields
  need a file to land in. That's the whole hard dependency.
- **Rollout dependency: Stories 1, 4, and 5 before telling users shared
  memory is the default.** Story 1 gives us the canonical record format.
  Story 4 gives users a gitignore/gitattributes template that fails closed
  on new directories and collapses diffs in PR review. Story 5 blocks
  `forge run` when catastrophic paths are tracked. Without all three,
  users can generate good per-run files but still get bitten by a stray
  `.forge/worktrees/` commit.
- **Performance dependency: Story 2 before any consumer relies on
  historical queries at scale.** The SQLite index turns O(N) file scans
  into indexed lookups. Until it lands, `_prior_approve_recorded` and
  telemetry aggregation stay on the direct-scan path, which is fine at
  current repo sizes but won't scale.

Stories 3 and 6 are independent of the above. Story 7 is a follow-up.
Story 8 is a metadata update.

The chain:

1. **Per-run audit files (Phase A — dual-write) with contract and
   redaction.** `_write_audit` writes `.forge/audits/runs/{run_id}.json`
   including `schema_version`, `parent_run_id`, `forge_version`, and the
   redaction pass, in addition to `history.jsonl` and `forge_audit.yaml`.
   No consumer changes. Tests verify both paths populate, records are
   immutable after write, and the redaction pass strips known secret shapes.

2. **SQLite index + consumer migration (Phase B).** Stand up
   `.forge/audits/index.sqlite` with staleness detection and cold-rebuild
   (`forge audits rebuild`). Switch telemetry, sprint rollup,
   `_prior_approve_recorded`, and the `assignment_history` readers from
   scanning `history.jsonl` to querying the index, with a mixed-version
   fallback to direct-scan `runs/*.json` on index miss. Each consumer is
   independently testable.

3. **`assignment_history.yaml` → derived view.** Delete the direct writer;
   readers switch to the index. The tracked file is removed from the
   template. Migration note for existing users: `forge audits rebuild`
   regenerates a local YAML view for operators who want it.

4. **Canonical gitignore template in `forge init`.** Write the full
   default-deny commented block. Generate `.gitattributes` with
   `linguist-generated=true` entries. Add `--shared-memory` /
   `--local-memory` flags. Idempotent: don't duplicate if the block already
   exists. Update `forge secrets-init` to use the same template path.

5. **Runtime precondition checks in `forge run`.** Extend the existing
   secrets-tracking check to the full blocker/warning list. Fail fast with
   actionable messages.

6. **Dogfood: align TheForge's own `.gitignore` and `.gitattributes`
   against the template.** Replace the current ad-hoc block. The dogfood
   config becomes the reference implementation users will look at. Spot-
   check recent `runs/*.json` for leaked secrets before flipping.

7. **Phase C cleanup — stop writing `history.jsonl`.** Drop the dual-write
   once Phase B is proven on a real sprint. Follow-up story, not in the
   initial chain.

8. **Rescope #307.** The existing "document filesystem layout" issue
   points at the canonical template and category model rather than
   reinventing the documentation from scratch.

Stories 1 and 2 are the substantive work. 3, 4, 5, 6 are mechanical once
the format is settled.

**Knowledge-capture Layer 1 can land as soon as Story 1 does**, writing
`run_id`, story text, and plan content into the per-run audit records.
No new storage decisions, just field additions. Stories 2–6 land in
parallel or shortly after, on the rollout and performance timelines
described above.

---

## Relationship to Knowledge Capture

Knowledge-capture Layer 1 (`docs/plans/knowledge-capture.md`) adds fields to
the audit record: story text, plan content, plan regeneration lineage,
`run_id`. This design note adds the *file format, location, index, and
policy* those fields land into.

The two land together as a coherent story chain:

```
forge-storage-layout (this doc)
  → Story 1: per-run audit files with contract + redaction (dual-write)
  → Story 2: SQLite index + consumer migration (with mixed-version fallback)
  → Story 3: assignment_history → derived view
  → Story 4: canonical gitignore + gitattributes + init flags
  → Story 5: runtime precondition checks
  → Story 6: dogfood
  → Story 7: Phase C cleanup (follow-up)
  → Story 8: rescope #307

knowledge-capture (sibling doc)
  → Layer 1: richer audit capture (writes into per-run files)
  → Layer 2: post-run summaries (.forge/knowledge/summaries/{run_id}.yaml)
  → Layer 3: feedback loop
```

Knowledge-capture Layer 1 has a hard implementation dependency on Story 1
and nothing else from this chain. Stories 2–6 are independent
improvements that land on the rollout and performance timelines described
in "Story Sequence" above, and they also unblock other downstream work
(adaptive assignment's routing flywheel queries the same index, for example).
