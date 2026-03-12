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

## Current State (v0.2)

```
INIT → WORKSPACE → PREFLIGHT → DEV → VALIDATE → REVIEW → HUMAN_REVIEW → DONE/ESCALATE
                                 ↑                  ↓              ↓
                                 └──── (REQUEST_CHANGES) ◄─── (reject)
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

**Spec:** `specs/multi-cli-support.md`

---

### Phase 2: Human-in-the-Loop ✓

**Status: Done.** `forge run --interactive` pauses at HUMAN_REVIEW.
Human can approve, reject (with findings fed back to dev), or escalate.
Non-interactive mode skips HUMAN_REVIEW. EOF on stdin → auto-escalate.

**Spec:** `specs/human-in-the-loop.md`

---

### Phase 3: Multi-Model Review Pool ✓

**Status: Done.** `forge.yaml` `profiles.review_pool` configures N
independent reviewers. Fan-out runs all reviewers on the same diff,
then synthesis reconciles into a single verdict. Per-profile budget
enforcement. Degraded mode when reviewers fail (skip synthesis if
only 1 succeeds).

**Spec:** `specs/multi-model-review.md`

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

### Phase 6: Auto-Merge

**Why next:** `forge run` produces a reviewed branch but merging is
still manual. An `--auto-merge` flag (separate from `--auto` / interactive)
would fast-forward merge to main after APPROVE, with safety checks
(branch protection, CI status, clean diff).

**Spec:** `specs/auto-merge.md` (planned)

---

### Phase 7: Campaign Mode

**Why after auto-merge:** Running specs one at a time is fine for
development, but reaching vision completion requires autonomous
multi-spec execution. A `forge campaign` command reads a `campaign.yaml`
manifest and runs specs sequentially, with per-spec and aggregate
budget gates.

**Key design constraints:**
- Campaign is a deterministic outer loop — no LLM decides ordering
- `--auto-merge` merges each spec's branch after APPROVE
- Budget enforcement is aggregate (stop if campaign ceiling hit)
- Claude-only for budget tracking until Codex/Gemini report costs
- DECOMPOSE (LLM-driven task splitting) is deferred — campaign specs
  are human-authored

**Spec:** `specs/campaign-mode.md` (planned)

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

4. **Unit tests:** 145+ tests in `tests/test_coordinator.py` covering all
   state transitions, budget enforcement, preflight verdicts, pool
   degradation, synthesis, human review, and edge cases. All tests mock
   subprocess — no real CLI invocations.

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

4. **Specs are the contract.** The dev agent's only input is the spec.
   If the spec is bad, the output will be bad. Spec quality is worth
   investing in.

5. **Process is not optional.** Worktrees, gates, handoffs, reviews —
   every shortcut generates cleanup debt. The coordinator enforces
   process mechanically so agents can't skip steps.
