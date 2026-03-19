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

## Current State (v0.2)

```
INIT → WORKSPACE → PREFLIGHT → DEV → VALIDATE → REVIEW → HUMAN_REVIEW → DONE/ESCALATE
                                 ↑                  ↓              ↓
                                 └──── (REQUEST_CHANGES) ◄─── (reject)

forge run --until <state>  stops the pipeline after reaching that state
  e.g. --until preflight   run spec classification only, no dev cycles
       --until dev          run through DEV+gate, skip review
       --until review        run through review, skip HUMAN_REVIEW
```

**Implemented:**
- Multi-CLI support: Claude, Codex (OpenAI), and Gemini runners
- Multi-model review pool with fan-out + synthesis reconciliation
- Preflight phase: one-shot spec classification (PROCEED/ALREADY_DONE/BLOCKED)
  before expensive dev+review cycles; fail-open design
- Human-in-the-loop: `forge run --interactive` pauses at HUMAN_REVIEW for
  approve/reject/escalate decisions; non-interactive mode skips to auto-behavior
- Live activity stream: real-time tool-use visibility via stream-json Popen
  (Claude); progress heartbeat for all CLIs (30s)
- Budget enforcement: per-profile cumulative cost ceilings (dev + per-reviewer);
  Claude-only for now (Codex/Gemini report cost_usd=0.0)
- Per-agent cost breakdown in audit logs with model usage detail
- Schema-enforced review output with cross-validation (APPROVE+P1 and
  REQUEST_CHANGES+no-P1 are always errors)
- Dirty-worktree detection between gate and review
- Stale handoff deletion before re-running gate

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

### Phase 11: Decompose

**Depends on:** Phase 9 + 10 (dependency analysis + parallel execution)

**Why last:** LLM-driven spec decomposition is the capstone. A large
spec gets split into sub-specs with annotated `file_scope`, which the
dependency analyzer groups into parallel batches automatically.

**Implementation path:** `forge ideate` is the implementation of DECOMPOSE
done right. Instead of a single LLM splitting a spec, the multi-model
deliberation protocol in `forge ideate` is applied to the question "what
sub-problems does this spec contain?" The converged output feeds directly
into a campaign manifest. To use `forge ideate` for decomposition:

```
forge ideate "What sub-problems should <high-level spec> be split into?"
```

The synthesized spec becomes the campaign manifest template. Human reviews
the ideation output before any `forge sprint` run is invoked — this is
the mandatory human gate in the decompose flow. The coordinator remains
fully deterministic; only the ideation agents are LLMs.

**What it means:**
- `forge ideate` applied to "what sub-problems does this spec contain?"
  produces a decomposition that the campaign runner can execute
- Each sub-spec in the ideation output has its own `file_scope`,
  acceptance criteria, and slug
- Sub-specs feed into the campaign runner (sequential or parallel)
- Human reviews the ideation output (mandatory gate) before execution
- The coordinator is still deterministic — only the ideation agents are LLMs

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

4. **Unit tests:** 175+ tests across `test_coordinator.py` and
   `test_campaign.py` covering all state transitions, budget enforcement,
   preflight verdicts, pool degradation, synthesis, human review, auto-merge,
   campaign execution, and edge cases. All tests mock subprocess — no real
   CLI invocations.

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
