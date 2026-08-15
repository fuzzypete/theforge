# Proposal: Compounding Engineering Memory for TheForge

Status: living — feeds the v0.14 knowledge feed-forward milestone.

From deterministic orchestration to a system that learns from its own work.

---

## The Observation

TheForge runs stories through a deterministic pipeline — plan, dev, validate,
review — and produces structured audit records for every run. But the audit
trail captures **process metrics** (cost, duration, iterations, verdicts)
while discarding the **reasoning inputs** that drove those metrics.

The result: every run starts from scratch. The system doesn't know what plans
worked for similar stories, what review findings recur, what failure modes
have already been diagnosed and solved, or what conventions the codebase has
evolved. Every agent rediscovers the same things.

This proposal turns TheForge from a system that **runs work deterministically**
into one that also **accumulates structured knowledge from every run** — and
feeds that knowledge back into future runs.

---

## Why Now

Three things are converging:

**Adaptive assignment needs historical signal.** The current adaptive
assignment model (`docs/guides/adaptive-assignment.md`) describes a flywheel:
"Run stories → Collect telemetry → Update performance table → Better routing."
That flywheel runs on process metrics alone today — model, phase, outcome,
cost, duration. But the router's real power comes from correlating outcomes
with *what was attempted*: story complexity, plan approach, domain, failure
mode. That requires capturing the inputs, not just the outputs.

**Story decomposition will produce ephemeral tasks.** Phase 11 in `vision.md`
describes document-in decomposition: brief → stories → sub-tasks → sprint.
Decomposed sub-tasks won't have stable file paths or GH issues — they're
generated artifacts. If the audit doesn't snapshot what was requested, those
runs leave no record of what was actually done.

**Independent convergence.** A colleague working on spec-driven agent workflows
arrived at the same architecture independently: bounded roles, deterministic
gates, auditability, and a compounding documentation layer. When two people
find the same shape without coordination, it's usually a constraint being
surfaced, not a trend being followed.

---

## What's Missing Today

### Inputs are discarded

| Artifact | What's captured | What's lost |
|----------|----------------|-------------|
| Story | `story_path` as string (serializes as `"None"` for issue-sourced stories) | Story text the agent actually saw; `github_issue` number |
| Plan | cost, duration, outcome (audit block at `audit.py:119`) | `plan_structured` content — approach, steps, files, risks |
| Plan lineage | `plan_finding_registry`, `plan_match_provenance`, `plan_regen_filter_audit` (review-side trajectory) | Per-attempt parsed plan content (overwritten in `state.plan_structured` on each regen) |
| Dev prompt | Nothing | What the agent was told |
| Review narrative | Finding registry (severity, disposition, description) | Reviewer reasoning beyond individual findings |

The audit tells you a run took 3 dev iterations and cost $18. It doesn't
tell you what the plan said, what the story asked for, or what the reviewer
was thinking.

### Runs are isolated

Each run is a closed system. The context assembler (`task/context_assembler.py`)
builds context packs from the structural index, CLAUDE.md and CONVENTIONS.md
files, story text,
plan output, and review findings — but only from the *current* run. It has
no mechanism to include knowledge from prior runs: solved problems, validated
approaches, recurring failure patterns, or codebase conventions learned through
experience.

### Knowledge lives in the wrong places

Some knowledge does accumulate, but in places that are hard to query:

- **Escalation history** (`assignment_history.yaml`) — records model failures,
  feeds adaptive assignment. This is the closest thing to compounding memory
  today, but it only covers model selection.
- **Worktree artifacts** (plan.md, traces, logs) — rich but ephemeral. Gone
  when the worktree is cleaned up.
- **JSONL history** (`history.jsonl`) — append-only, complete, but stores
  only process metrics per run.
- **Git history** — the ultimate record of what changed, but has no link back
  to why it was changed or what was learned.

---

## The Proposal: Three Layers

### Layer 1 — Capture What We're Already Discarding

Small, scoped changes to existing audit serialization. No new phases, no new
config, no new artifacts. Pure data preservation.

#### Data contract

Layer 1 adds the following fields to the audit record produced by
`generate_audit_log()`:

```yaml
# ── task block (existing, extended) ──────────────────────────
task:
  name: string
  slug: string
  story_path: string | null          # fixed: currently serializes as "None" for issue-sourced
  story_text: string                 # the text the agent actually saw (snapshotted at load time)
  github_issue: int | null           # link back to source issue

# ── run identity (new) ───────────────────────────────────────
run_id: string                       # already exists in StructuredLogger; surface in audit record

# ── plan block (existing, extended) ──────────────────────────
plan:
  cost_usd: float
  duration_s: float
  outcome: string
  plan_structured: PlanData | null   # the final parsed plan: approach, steps, files, risks
  attempt_plans:                     # per-attempt plan snapshots (only populated on regen)
    - attempt: int                   # 0-indexed
      plan: PlanData | null          # parsed plan for this attempt
```

**What is explicitly out of scope for Layer 1:**

- Raw dev prompts and full agent responses. These may contain secrets,
  large pasted logs, or unbounded content. They remain in per-run log
  files under `.forge/logs/{slug}/` where they already live.
- Review narrative beyond the finding registry. The finding registry
  already captures description, severity, disposition, cycle tracking,
  and reporter identity — that's sufficient structured data for Layer 3
  correlation without introducing freeform text.

#### Implementation details

**Story text plumbing.** `generate_audit_log(config, task, result)` does not
currently receive `story_content`. The story is loaded in `engine.py` (line
534: `story_content = task.story_text if task.story_text is not None else
load_story(task.story_path)`) and in `run_setup.py` (line 276). Two options:

- **(a) Store in state** — `CoordinatorState` gets a `story_content: str | None`
  field, set at load time. `generate_audit_log` reads from `state`.
- **(b) Re-read at audit time** — `audit.py` reads `task.story_path` if
  `task.story_text` is None.

Option (a) is better: it captures what the agent actually saw, not what the
file contains at audit time (the story could be edited mid-run in an
interactive session, or the worktree could be in a different state).

**`story_path` serialization fix.** Currently `str(task.story_path)` produces
the literal string `"None"` for issue-sourced stories. Fix to emit JSON null.

**`run_id` in audit.** The run ID is generated in `run_setup.py` (line 218:
`_run_id = run_id or _cu._generate_run_id()`) and passed to `StructuredLogger`,
but is not included in the audit record. Add it as a top-level field. This
becomes the stable identity key for Layer 2 summary storage.

**Plan attempt snapshots.** The plan review trajectory fields
(`plan_finding_registry`, `plan_match_provenance`, `plan_regen_filter_audit`,
`plan_attempt_metadata`) already capture the *review-side* of each regen
cycle. What's missing is the *plan-side*: the parsed plan content per attempt.
Currently `state.plan_structured` is overwritten on each successful regen
(`plan_flow.py` lines 922-924).

Add `plan_attempt_plans: list[PlanData | None]` to `CoordinatorState`. Append
`state.plan_structured` before overwriting on regen. This complements the
existing trajectory fields without duplicating them — they track review
decisions, this tracks what was reviewed.

Serialize as `attempt_plans` in the plan audit block, alongside the existing
`plan_attempt_metadata`.

#### Layer 1 cost

~40 lines across `audit.py`, `state.py`, `plan_flow.py`, `engine.py`.
No new dependencies. Backward-compatible — existing audit consumers ignore
unknown keys. Extends existing audit-generation tests to assert new fields.

---

### Layer 2 — Structured Run Summaries

After REVIEW → DONE, the coordinator emits a structured knowledge artifact:
a run summary that distills what happened into a form useful for future runs
and human review.

#### What the summary contains

```yaml
run_summary:
  what_changed:
    description: "Added retry logic to API client with exponential backoff"
    files_modified: ["src/client.py", "tests/test_client.py"]
    approach: "Wrapped existing call sites in retry decorator; added config..."

  what_was_learned:
    - claim: "API timeout handling requires both connection and read timeouts"
      evidence:
        - type: review_finding
          finding_id: "f-003"
          description: "P1: missing read timeout on retry path"
        - type: plan_step
          step_id: "s-2"
          description: "Step 2: add configurable timeout pair"
    - claim: "This codebase uses decorator pattern for cross-cutting concerns"
      evidence:
        - type: file
          path: "src/decorators.py"
          description: "Existing @rate_limit, @cache, @log_calls decorators"

  review_insights:
    recurring_findings:
      - finding_id: "f-007"
        description: "Missing type annotations on public API"
        cycles_seen: 3
    resolved_findings:
      - finding_id: "f-003"
        description: "Race condition in connection pool"
        resolution: "Fixed via context manager in pool.acquire()"

  complexity_signal:
    actual_iterations: 2
    plan_regenerations: 1
    dominant_difficulty: "edge case coverage"
```

#### Provenance is mandatory

Every claim in `what_was_learned` must include an `evidence` list linking it
to concrete artifacts from the run: finding IDs, plan step IDs, file paths,
diff hunks, or review cycle numbers. This is the guard against hallucinated
institutional memory. A summary agent that can't cite evidence for a claim
should omit the claim.

The schema validator rejects `what_was_learned` entries with empty `evidence`
lists.

#### How it's generated

This is a **bounded agent task** — summarize, don't decide. The coordinator
provides the plan, story, diff, review findings, and iteration history. The
agent returns structured YAML. The coordinator validates schema and persists.

This fits the existing model: LLMs generate artifacts, the coordinator
validates them mechanically. The summary is an artifact like any other —
schema-enforced, auditable, stored in the run record.

#### Where it lives

- Written to `.forge/knowledge/summaries/{run_id}.yaml` for querying across
  runs. Keyed by `run_id` (not slug) to avoid collisions across reruns of
  the same story.
- Linked to — not inlined in — the authoritative run record. The summary
  carries `authoritative_run_record: .forge/audits/runs/{run_id}.json`; the
  record carries no forward pointer, because it is written first and is
  immutable once written. **The forward direction is resolved by the same path
  convention**: a run's summary, if it has one, is at
  `.forge/knowledge/summaries/{run_id}.yaml`. Layer 3 consumption reads it
  there rather than expecting a field on the record.

#### As implemented (#1859)

The artifact splits along who can be trusted with what. The agent supplies
`what_changed.description`/`approach`, `what_was_learned`, `learned_patterns`,
`review_insights` observations, and `complexity_signal.dominant_difficulty`.
Every countable field — `changed_files`, `domains`, `story_shape`,
`review_insights.recurring_findings`/`resolved_findings`, and the iteration /
cycle / regeneration / cost entries of `complexity_signal` — is filled in by the
coordinator from the audit record, so no index built over these artifacts can be
poisoned by a model's recollection.

Evidence is validated by *resolution*, not by shape: each item's reference must
exist in the run's own audit (finding registry, plan steps, review cycles,
changed-file paths, diff refs). A well-formed citation of a finding the run
never raised is rejected, and one unresolvable citation rejects the whole
summary.

Generation dispatches on a tool-free API transport derived from `plan.ref` (via
its `api_fallback` when the plan model is a CLI). An empty tool allowlist is
only narrow on the API path — the Claude CLI reads it as "no restriction" — so a
plan model with no API transport available skips generation rather than
dispatching an unbounded agent. Its cost is recorded on the artifact under
`generation.cost_usd`, since it is spent after the run's own cost accounting has
closed.

#### When it runs

**Optionally, after DONE.** Not on every run — configurable in `forge.yaml`:

```yaml
knowledge:
  run_summaries: true       # produce and accumulate summaries
  prior_run_context: false  # inject them into future agent prompts
```

Generation and consumption are **separate knobs with separate readiness
points**, and both ship disabled. `run_summaries` can be enabled as soon as
Layer 2 lands, so a corpus accumulates while nothing consumes it.
`prior_run_context` gates Layer 3 injection and stays off until the
admissibility classifier exists — see ADR-0002 clause 5. One switch
controlling both would make it impossible to build the corpus that retrieval
and admissibility have to be tested against.

Cost is minimal: one bounded agent call with constrained output. Estimated
$0.10-0.30 per run using a mid-tier model. The summary phase is **not
load-bearing** — if it fails, the run still succeeded. The DONE transition
is already committed; summary generation is a post-DONE side effect, not a
gate.

---

### Layer 3 — Knowledge Feedback Loop

This is where it compounds. Prior run summaries feed into future runs
through the existing context assembly machinery.

#### 3a. Knowledge index

Summaries accumulate in `.forge/knowledge/summaries/`. A lightweight index
tracks them by domain, file, pattern, and recency:

```yaml
# .forge/knowledge/index.yaml (derived, rebuildable, disposable)
entries:
  - run_id: "20260410-143022-api-retry"
    slug: "api-retry-logic"
    domain: "backend-api"
    files: ["src/client.py"]
    patterns: ["retry", "timeout", "exponential-backoff"]
    timestamp: "2026-04-10T14:30:00"
    summary_path: ".forge/knowledge/summaries/20260410-143022-api-retry.yaml"
```

The index is **rebuilt deterministically from summaries** — no LLM in the
loop. Simple keyword extraction from `what_changed.description`,
file paths from `files_modified`, and domain from preflight classification
(already captured in audit). The index is a materialized view, not a source
of truth. It can be deleted and rebuilt at any time from the summary files.

#### 3b. Context assembly integration

The context assembler (`task/context_assembler.py`) already builds
phase-specific context packs with budget-aware inclusion/exclusion and
required/advisory separation. Knowledge items become a new advisory source
alongside structural index, CLAUDE.md, CONVENTIONS.md, and plan output:

```python
ContextItem(
    source="knowledge:20260410-143022-api-retry",
    kind="prior_run_summary",
    required=False,        # advisory — droppable under budget pressure
    lines=12,
    content="...",
    reason="Prior run modified src/client.py (file overlap with current plan)",
    score=75,              # relevance: recency + file overlap + domain match
)
```

The assembler scores knowledge items by relevance (file overlap with current
story's plan, domain match, recency) and includes the top N within the
phase's line budget. Low-relevance items get dropped — same as any other
advisory context item. The manifest records what was included and what was
dropped, maintaining full audit visibility into what knowledge the agents
actually received.

#### 3c. Phase-specific knowledge injection

Different phases benefit from different knowledge:

| Phase | Knowledge type | Example |
|-------|---------------|---------|
| Preflight | Complexity signals from similar stories | "Stories touching src/client.py averaged 2.5 iterations" |
| Plan | Approaches that worked/failed for related changes | "Prior retry implementation used decorator pattern" |
| Dev | Conventions and patterns from this codebase | "This codebase uses X pattern for cross-cutting concerns" |
| Review | Recurring findings in this area | "Type annotations on public API flagged in 3 prior reviews" |

#### 3d. The flywheel

```
Run story
  → Capture inputs (Layer 1)
  → Generate summary (Layer 2)
  → Index summary (Layer 3a)
  → Next run's context includes relevant summaries (Layer 3b)
  → Agents start with knowledge, not from scratch
  → Better plans, fewer iterations, fewer rediscovered findings
  → Richer summaries from higher-quality runs
  → Repeat
```

After 10-20 runs against a repo, the knowledge index contains a useful
map of the codebase as experienced through actual work: which areas are
tricky, what conventions matter, what patterns recur, what approaches
work. This is not documentation — it's institutional memory earned through
practice.

#### 3e. Feeding the adaptive router

Adaptive assignment (`docs/guides/adaptive-assignment.md`) consumes
**deterministic metrics and structured inputs from the audit record**, not
freeform summary claims. Layer 1 enriches the router's data model from:

```
{model, phase, outcome, cost, duration, p1_count, cycles}
```

to:

```
{model, phase, outcome, cost, duration, p1_count, cycles,
 story_complexity, story_domain, plan_approach, plan_regen_count,
 file_count, acceptance_criteria_count}
```

This enables correlations the current data can't support:

- "Stories with >5 acceptance criteria in the backend-api domain average
  3.2 iterations with sonnet, 1.8 with opus"
- "Plan regeneration rate for this complexity class is 40% with mid-tier,
  10% with strong-tier"
- "Review cycle count correlates with plan quality score more than with
  dev model tier"

The router queries deterministic fields, not LLM-authored summaries. This
is an important boundary: **the router trusts data the coordinator produced;
it does not trust claims an LLM made about that data.** Summaries inform
agents (which are already LLMs operating under bounded roles); the router
operates on facts.

---

## What This Proposal Does NOT Include

**Docs reorganization.** No new `docs/solutions/`, `docs/patterns/`,
`docs/decisions/` directories. Human-facing documentation structure is a
separate concern. The knowledge captured here is machine-readable run data,
not prose documentation.

**Target-repo documentation generation.** Generating docs *about* the repo
TheForge is operating on (architecture docs, API references, etc.) is a
different capability. This proposal captures knowledge *from runs*, not
knowledge *about codebases*.

**LLM-driven process decisions.** Summaries are artifacts, not routing
inputs. The knowledge index is built deterministically. Context assembly
scores items with simple heuristics. The coordinator remains pure Python
throughout.

**New pipeline states.** The summary generation in Layer 2 is a post-DONE
step, not a new state machine state. If it fails, the run is still DONE.

**Raw prompt or response capture.** Dev prompts, full agent responses, and
review narratives may contain secrets, large pasted content, or unbounded
text. These remain in per-run log files. Layer 1 captures structured,
bounded inputs (story text, parsed plan data) — not raw I/O.

---

## Implementation Sequence

| Layer | Scope | Effort | Depends on |
|-------|-------|--------|------------|
| 1a: Story text + issue in audit | `audit.py`, `state.py`, `engine.py` | ~15 lines | Nothing |
| 1b: `story_path` null fix | `audit.py` | ~3 lines | Nothing |
| 1c: `run_id` in audit | `audit.py` | ~3 lines | Nothing |
| 1d: Plan content in audit | `audit.py` | ~5 lines | Nothing |
| 1e: Plan attempt snapshots | `state.py`, `plan_flow.py`, `audit.py` | ~20 lines | Nothing |
| 2: Run summaries | Schema, summary agent, config, storage | Medium story | Layer 1 |
| 3a: Knowledge index | Index builder, storage | Small story | Layer 2 |
| 3b: Context integration | `context_assembler.py`, scoring | Medium story | Layer 3a |
| 3c: Phase-specific injection | Prompt builders in `task/` | Medium story | Layer 3b |
| 3d: Router integration | Router queries against enriched audit data | Medium story | Layer 1 + router |

Layer 1 is a single story — five small changes, ~45 lines total, no new
dependencies. Ship it, start accumulating richer audit data immediately.

Layer 2 is a story. Needs schema design for summaries (with provenance
validation), a bounded agent call, `forge.yaml` config key, and storage
under `.forge/knowledge/summaries/{run_id}.yaml`.

Layer 3 is 2-3 stories: index builder, context assembly integration, and
phase-specific prompt changes. Each builds on the previous but is
independently useful.

Router integration (3d) can happen in parallel with Layer 3 — it depends on
Layer 1 data but not on summaries.

---

## Success Criteria

The knowledge system compounds if:

- **Rediscovery drops.** Agents stop re-learning the same conventions,
  patterns, and failure modes. Measurable: compare iteration counts for
  stories touching files that have prior run summaries vs. files that don't.

- **Plan quality improves.** Plans that draw on prior approaches get fewer
  REGEN cycles. Measurable: plan regeneration rate over time.

- **Review findings converge.** Recurring findings get addressed proactively
  in dev rather than caught in review. Measurable: restated-finding rate
  across runs.

- **Router accuracy improves.** Model selection based on story
  characteristics outperforms static tier tables. Measurable: first-attempt
  success rate.

- **Sprint velocity increases.** The same budget buys more completed stories
  over time because agents start smarter. Measurable: stories/dollar trend.

If these metrics don't move after 20-30 runs, the knowledge layer isn't
earning its keep and should be simplified or removed. The system should
prove its value empirically, not be maintained on faith.

---

## Bottom Line

TheForge already has the execution loop, the audit infrastructure, and the
context assembly machinery. The gap is that runs don't learn from each other.

Layer 1 closes the data gap — stop discarding inputs.
Layer 2 distills runs into reusable knowledge — evidence-backed summaries.
Layer 3 feeds knowledge back into future runs — the compounding effect.

The end state: every story TheForge runs leaves the system better equipped
for the next one. Not because someone wrote documentation, but because the
system's own work is its training data.
