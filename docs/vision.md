# TheForge Vision

## What TheForge Is

A deterministic multi-LLM development orchestrator. The coordinator is pure
Python — no LLM in the loop for process decisions. Agents write code and
write reviews. The coordinator validates boundaries mechanically.

## What TheForge Replaces

The manual workflow that Paul runs across Claude, Codex, and Gemini sessions:

```
Observe → Frame → Think (Opus) → Spec → Dev (Sonnet) → Gate → Review → Fix → Cross-review → Merge
```

TheForge doesn't replace the human — it replaces the copy-paste, the
handoff formatting, the "create a tree and crank through it" busywork.
The human stays at every decision point that matters.

---

## Upstream Workflow: Brief → Story → Plan → Dev

TheForge operates on _stories_ — markdown files that describe WHAT and WHY.
Stories are not implementation tasks; they contain acceptance criteria that
define observable, testable behavior.

```
Brief (human) → Story (forge ideate / human) → Plan (PLAN phase) → Dev (DEV phase)
```

| Artifact | Author | Contains |
|----------|--------|----------|
| Brief | Human | One-paragraph problem statement |
| Story | Human or `forge ideate` | Problem context + acceptance criteria |
| Plan | Planning agent (PLAN phase) | Implementation approach, file changes, risks |
| Code | Dev agent (DEV phase) | Implementation satisfying every AC |

**A story says WHAT and WHY. The plan says HOW. The dev agent implements the HOW.**

The dev prompt includes a preamble that tells agents:
- Acceptance criteria are the definitive checklist
- Context and background sections are informational, not requirements
- Ambiguity should be flagged in `dev_notes`

Story files live in `specs/` by convention (e.g. `specs/my-feature.md`) and are
passed to `forge run <story-file>`. `forge init` creates `specs/TEMPLATE.md` with
annotated structure so new projects have a reference for what a well-written story
looks like.

---

## Current State (v0.3)

```
INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW → DONE/ESCALATE
                                                       ↑                  ↓
                                                       └──── (REQUEST_CHANGES)
```

**Core pipeline:**
- Multi-CLI support: Claude Code, Codex CLI, and Gemini CLI as subprocess agents
- API-mode agents: OpenAI, Anthropic, Google, and DeepSeek via HTTP with
  TheForge providing the tool runtime (Read, Edit, Write, Bash, Glob, Grep)
- Plan phase: planning agent produces implementation plan before dev starts;
  multi-model plan review pool catches structural issues early
- Multi-model review pool with deterministic fan-out + synthesis reconciliation
- Preflight phase: one-shot spec classification (PROCEED/ALREADY_DONE/BLOCKED)
- Budget enforcement: per-profile cumulative cost ceilings with token-level
  cost tracking for API-mode agents
- Schema-enforced review output with cross-validation
- Stale worktree detection: `forge run --resume` triages existing worktrees
  and resumes from the correct phase

**Agent loop (API mode):**
- Full tool-use agent loop with iteration and time-based nudges
- Forced finalization on timeout (provider-specific constrained output)
- Per-profile `max_iterations` with nudge at 80%
- Time-based nudge at 80% of wall-clock deadline
- Connection-level HTTP timeout enforcement

**Operational:**
- Sprint mode: `forge sprint` runs multiple stories sequentially with shared budget
- Multi-LLM ideation: `forge ideate` for collaborative spec generation
- Provider smoke test: `forge check-providers` verifies connectivity
- Per-run verbose log capture (stderr tee to `.forge/logs/`)
- Per-agent cost breakdown with model usage detail in audit YAML
- Structured event logging (JSONL) with phase-level timing
- ntfy/osascript notifications on completion

---

## Roadmap

### Phase 1: Multi-CLI Support ✓

**Status: Done.** Runner dispatches to Claude, Codex, and Gemini CLIs.
Each has its own command builder, output parser, and heartbeat.
Config validates CLI names at load time. Cost tracking is Claude-only
for now (Codex/Gemini report 0.0).

**Spec:** `specs/archive/multi-cli-support.md`

---

### Phase 2: Human-in-the-Loop ✓

**Status: Done.** `forge run --interactive` pauses at HUMAN_REVIEW.
Human can approve, reject (with findings fed back to dev), or escalate.
Non-interactive mode skips HUMAN_REVIEW. EOF on stdin → auto-escalate.

**Spec:** `specs/archive/human-in-the-loop.md`

---

### Phase 3: Multi-Model Review Pool ✓

**Status: Done.** `forge.yaml` `profiles.review_pool` configures N
independent reviewers. Fan-out runs all reviewers on the same diff,
then synthesis reconciles into a single verdict. Per-profile budget
enforcement. Degraded mode when reviewers fail (skip synthesis if
only 1 succeeds).

**Spec:** `specs/archive/multi-model-review.md`

---

### Phase 4: Preflight Spec Validation ✓

**Status: Done.** One-shot classification call before dev cycles.
Verdicts: PROCEED (continue), ALREADY_DONE (skip to DONE), BLOCKED
(escalate with reason). Fail-open: if the agent fails or output is
unparseable, default to PROCEED. Reads file_scope contents from main
branch and checks every acceptance criterion individually.

---

### Phase 5: Live Activity Stream ✓

**Status: Done.** Claude runner uses `subprocess.Popen` with
`--output-format stream-json --verbose` for real-time JSONL events.
Tool-use summaries printed to stderr as they happen. All CLIs have
30s progress heartbeat.

---

### Phase 6: Auto-Merge ✓

**Status: Done.** `forge run --auto-merge` merges the feature branch
into the base branch after APPROVE. Fast-forward preferred, regular
merge fallback. Safety checks: base branch exists, no uncommitted
changes in project root, branch has commits ahead of base. Worktree
cleanup attempted after successful merge. Merge outcome recorded in
audit log.

**Spec:** `specs/archive/auto-merge.md`

---

### Phase 7: Campaign Mode ✓

**Status: Done.** `forge sprint campaign.yaml` runs specs sequentially
through the full pipeline. `campaign.yaml` manifest defines ordered spec
list and aggregate budget ceiling. Budget enforcement is Claude-only
(Codex/Gemini report $0.00) — warning logged at start. Continues after
individual spec failures. ALREADY_DONE specs counted as skipped.
Writes `sprint-audit.yaml` with per-spec outcomes and costs.

**Spec:** `specs/archive/campaign-mode.md`

---

### Phase 8: Cross-Project Support

**Why later:** TheForge currently works for theforge (dogfooding). Making
it work for other projects proves generality.

**What it means:**
- Document the forge.yaml schema fully
- Test with a real external project
- Handle project-specific quirks:
  - Different test runners (pytest, vitest, jest)
  - Different lint tools (ruff, eslint)
  - Monorepo structures
  - Docker dependencies

**Spec:** `specs/cross-project-support.md`

---

### Phase 9: Spec Dependency Analysis

**Why before parallel:** Parallel execution is unsafe without knowing
which specs conflict. This phase adds static analysis of `file_scope`
across a campaign manifest to build a dependency graph.

**What it means:**
- Compare `file_scope` across all specs in a campaign manifest
- Identify independent specs (disjoint file scopes) vs conflicting ones
- Build an execution graph: independent specs can be grouped into
  parallel batches, overlapping specs must be serialized
- Warn on specs with empty `file_scope` (unrestricted = conflicts with
  everything)
- Output: execution plan showing which specs run in which batch

---

### Phase 10: Parallel Campaign Execution

**Depends on:** Phase 9 (Spec Dependency Analysis)

**Why after dependency analysis:** Running concurrent worktrees without
conflict detection produces merge failures. With the dependency graph
from Phase 9, the campaign runner can safely parallelize independent
specs while serializing conflicting ones.

**What it means:**
- Campaign runner groups independent specs into parallel batches
- Each batch runs specs concurrently in separate worktrees
- Batches execute sequentially (batch 1 merges before batch 2 starts)
- Aggregate budget enforcement across parallel specs
- Per-spec audit still works; campaign audit shows batch structure

---

### Phase 11: Upstream Orchestration — Brief to Sprint Plan

**Depends on:** Phase 9 + 10 (dependency analysis + parallel execution)

**Why this matters:** TheForge currently starts at the story level and
assumes a human already decomposed the work. The real bottleneck isn't
dev or review — it's the upstream work of turning a vision into
well-scoped, dependency-aware sprints. This phase brings that upstream
workflow into the forge.

**Core UX: document-in, forge figures it out.**

The user doesn't pick a command per artifact type. They drop a document
into forge and it classifies what it is, enters the pipeline at the
right stage, and exits after the requested stage (or runs to completion):

```
forge run doc.md
  → classify: "this looks like a brief" (or story, or epic, or sprint)
  → HITL confirm: "Detected: brief. Start from ideation? [Y/n]"
  → enter pipeline at the right stage
  → exit after requested stage (default: one stage, --through=dev for full)
```

**The full lifecycle:**

```
Brief (vision)
  → Stories (forge ideate / human)
    → Grooming (too big? too vague?)
      → Epic (promotes oversized story)
        → Sub-stories (decomposed from epic)
          → Dependency discovery (across all stories)
            → Sprint plan (parallel tracks within each sprint)
              → HITL gates (scope/rescope decisions)
                → Execution (forge sprint)
```

**Document classification** is lightweight — structure detection, not
LLM inference. A brief has no acceptance criteria, a story has ACs but
no sub-stories, an epic references child stories, a sprint has a
`stories:` list. When ambiguous, HITL confirms. When unambiguous, forge
proceeds (with `--confirm` flag to force the gate).

**Key insight:** This is not one feature — it's a pipeline of
transformations with HITL decision points at each stage:

| Stage | Input | Output | HITL Gate |
|-------|-------|--------|-----------|
| Classify | Any doc | Detected type + entry point | Human confirms (if ambiguous) |
| Ideate | Brief | Candidate stories | Human selects/edits stories |
| Groom | Stories | Sized stories + epics | Human approves scope |
| Decompose | Epic | Sub-stories with ACs | Human validates split |
| Dependency | All stories | Dependency graph | Human resolves ambiguity |
| Plan | Graph + budget | Sprint sequence | Human approves sprint plan |
| Execute | Sprint file | Code + reviews | Existing forge pipeline |

**Single entry point, multiple exit points:**

```bash
forge run vision.md                    # classify → ideate → stop (outputs stories)
forge run vision.md --through=plan     # classify → ideate → groom → dep → plan → stop
forge run vision.md --through=dev      # full pipeline: classify → ... → sprint → dev
forge run story.md                     # classify as story → enters at dev (current behavior)
forge run sprint.yaml                  # classify as sprint → enters at sprint (current behavior)
```

**Implementation path:**

1. **Document classifier** — Structural detection (frontmatter keys,
   AC presence, sub-story references) with HITL fallback. Not an LLM
   call — deterministic rules in the coordinator.

2. **Stage-aware pipeline** — The coordinator gains an entry-point
   parameter and a stop-after parameter. Current `forge run story.md`
   is equivalent to `--enter=dev --through=done`.

3. **`forge ideate` as the decomposition engine** — Multi-model
   ideation applied to "what stories does this brief need?" and
   "what sub-stories does this epic need?" Same deliberation protocol,
   different prompt.

4. **`forge plan-sprint`** — Analyzes dependencies across stories,
   groups into parallel tracks, sequences into sprints. Respects
   `depends_on` (explicit) and file-scope overlap (inferred).
   Output: sprint YAML files ready for `forge sprint`.

5. **HITL at every stage** — The coordinator is still deterministic.
   LLMs propose, humans approve. Each stage writes its output to disk
   and pauses for human review before the next stage begins. No
   autonomous multi-stage execution without explicit human opt-in.

**What "lightweight HITL at escalate" enables here:** When decomposition
produces ambiguous splits or sizing disagreements between models, a
synthesis model presents the human with structured options ("Model A
suggests 3 stories, Model B suggests 5 — here's the tradeoff") rather
than dumping raw outputs. This is the same pattern needed for dev
escalation and can be built once and reused.

**Relationship to `forge ideate`:** Ideation is the engine for stages
1-3. The deliberation protocol (independent generation → cross-review
→ convergence detection) applies to decomposition the same way it
applies to spec writing. The coordinator remains fully deterministic;
only the ideation agents are LLMs.

---

## Enhancement Queue

Findings from real-world usage (dogfooding + external project testing).
These are independent of the phased roadmap and can be picked up anytime.

### Timeout Handling

- **Graceful timeout warning:** Agent sessions that approach the timeout
  limit should receive a warning (e.g. at 80% of timeout) so the agent
  can wrap up cleanly instead of being hard-killed mid-edit.
- **Timeout auto-tuning:** Track actual dev session durations per spec
  complexity (file count, spec length). Surface recommendations in audit:
  "This spec took 18min; timeout was 15min — consider increasing."
- **Resume after timeout:** If a dev session is killed by timeout, the
  coordinator could resume from the worktree state rather than starting
  fresh, preserving partial progress.

### Open P2s (from dogfooding)

- **Merge logic 3x duplication:** `coordinator.py` has the same merge
  block at interactive-approve, non-interactive-approve, and
  human-approve-after-exhausted-cycles. Extract to shared helper.
- **Frontmatter parsing duplication:** `_build_task_from_spec()` in
  `campaign.py` duplicates `_parse_spec_frontmatter()` in `cli.py`.
  Consolidate into a shared function.
- **Double manifest load:** `cli.py` `cmd_sprint` loads the manifest,
  then `run_sprint()` loads it again. Pass it through instead.
- **Missing failed-merge campaign test:** No test for what happens when
  auto-merge fails mid-campaign.

### Cross-Project Learnings (from HDP)

- **Gate command complexity:** External projects have sophisticated gate
  commands (`make fmt && make lint && pytest`). Gate failures need better
  diagnostics — which step failed? Currently it's just PASS/FAIL.
- **Worktree bootstrapping:** External projects may need dependency
  installation in the worktree (poetry install, npm install). Consider
  a `workspace.setup_command` in forge.yaml.
- **Large spec handling:** Specs with 30+ entities (exercises, endpoints,
  etc.) push Sonnet sessions long. May need spec decomposition guidance
  or automatic splitting.

### Multi-Model Dev: Fallback Escalation

Current: dev agent is a single model. When it exhausts retries, it
escalates to human. A smarter retry policy: escalate to a stronger
model before involving the human.

```
iter 1-2: Sonnet (fast, cheap)
iter 3:   Codex or Opus (stronger, slower)
escalate: human
```

Configurable in forge.yaml as a `dev_fallback` profile. Coordinator
switches profiles on retry exhaustion, not on every iteration.

### Multi-Model Ideation (IDEATE stage)

Multi-model competition is most valuable upstream of implementation,
proportional to spec ambiguity. The pattern that works:

```
Phase 1: each model produces ideas independently (no cross-contamination)
Phase 2: each model reviews all other models' outputs
Phase 3: coordinator detects convergence (same conclusion in N of M)
         → converged items become spec inputs
         → divergent items iterate again
         → residual divergence → human executive decision
```

This is structurally identical to the review pool — Phase 2 is
cross-review, Phase 3 is synthesis. The difference is the output:
IDEATE produces structured ideas/constraints, not code.

**Where it fits in the pipeline:**

| Stage | Ambiguity | Multi-model value |
|-------|-----------|-------------------|
| Ideation | High | Maximum — deliberation protocol |
| Spec writing | Medium | Moderate — single model fine if IDEATE was thorough |
| Implementation (DEV) | Low | Low — single strong model + gate |
| Review | Zero (what was built) | Maximum — independent blind-spot coverage |

**Relationship to DECOMPOSE (Phase 11):** IDEATE is DECOMPOSE done
right. Instead of one LLM splitting a spec, the deliberation protocol
runs on "what are the sub-problems", converges, then emits the spec
list for the campaign runner. The coordinator remains deterministic —
only the ideation agents are LLMs.

---

## Testing Strategy

1. **Dogfood loop:** Forge develops itself. Specs in `specs/` are run
   through `forge run` to implement features in TheForge. This has
   been validated end-to-end: multi-CLI, multi-model review, HITL,
   live activity, and preflight were all implemented or tested via
   dogfooding.

2. **Preflight as regression guard:** Stale specs (already implemented)
   are caught by PREFLIGHT → ALREADY_DONE, avoiding wasted dev+review
   cycles. Budget-enforcement and audit-improvements specs confirmed this.

3. **Multi-model review:** `forge.yaml` configures Claude + Codex + Gemini
   review pools. Tested with real cross-CLI reviews.

4. **Unit tests:** 900+ tests across 29 test files covering all state
   transitions, budget enforcement, preflight verdicts, pool degradation,
   synthesis, human review, auto-merge, sprint execution, API agent loops,
   provider adapters, tool runtime, and edge cases. All tests mock
   subprocess and HTTP — no real CLI or API invocations.

---

## Principles

1. **The coordinator is not an LLM.** Every state transition is
   deterministic Python. If an LLM is deciding whether to retry, the
   architecture is wrong.

2. **The human is the final gate.** No code reaches main without human
   approval. Agents can review each other, but the human decides.

3. **Multi-model for independence.** The value of Codex reviewing
   Claude's code is that Codex has different blind spots. Same-model
   review catches bugs; cross-model review catches assumptions.

4. **Stories are the contract.** A story (the markdown file passed to
   `forge run`) is the dev agent's only input. The acceptance criteria in
   that story are the definitive checklist — context sections are not
   requirements. If the story is bad, the output will be bad. Story quality
   is worth investing in.

5. **Process is not optional.** Worktrees, gates, handoffs, reviews —
   every shortcut generates cleanup debt. The coordinator enforces
   process mechanically so agents can't skip steps.
