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

## Current State (v0.1)

```
INIT → WORKSPACE → DEV → VALIDATE → REVIEW → DONE/ESCALATE
```

- Single CLI: `claude`
- Single reviewer (or multi-model pool with synthesis)
- No human-in-the-loop — ESCALATE is a dead end
- No spec validation before dev starts
- No cross-CLI review (Codex, Gemini)
- Progress heartbeat (30s)

---

## Roadmap

### Phase 1: Multi-CLI Support

**Why first:** This is the single biggest unlock. The whole point of
multi-model review is independent perspectives from different model
families. Claude reviewing Claude's code is better than nothing, but
Claude + Codex + Gemini reviewing Claude's code is the actual goal.

**What it means:**
- `runner.py` gains a CLI dispatch layer: given a `ModelProfile`, select
  the right subprocess invocation
- Each CLI has its own:
  - Command builder (how to invoke it)
  - Output parser (how to extract the result)
  - Cost tracker (different billing models)
  - Tool mapping (each CLI names tools differently)
- `SUPPORTED_CLIS` expands: `{"claude", "codex", "gemini"}`
- Config validation ensures the CLI is installed/available

**CLI specifics:**

```yaml
# Claude: current implementation
claude --model sonnet --max-turns 50 --budget-tokens 50000 -p "prompt"

# Codex: OpenAI's CLI agent
codex --model o4-mini --full-auto -q "prompt"

# Gemini: Google's CLI agent
gemini-cli --model gemini-2.5-pro "prompt"
```

Each has different:
- Auth mechanisms (API keys, OAuth)
- Output formats (stdout, files, structured JSON)
- Tool permission models
- Cost reporting

**Spec:** `specs/multi-cli-support.md`

---

### Phase 2: Human-in-the-Loop

**Why second:** Right now ESCALATE is a dead end. In practice, Paul
IS the escalation handler — he reads the audit, gives P1/P2 findings,
and tells the agent to fix them. This should be a first-class state.

**What it means:**
- New phase: `HUMAN_REVIEW` between REVIEW and DONE
- `forge run --interactive` pauses at HUMAN_REVIEW and waits for input
- Input can be:
  - `approve` → DONE
  - `reject <findings>` → back to DEV with findings as context
  - `escalate` → ESCALATE (truly give up)
- Non-interactive mode: `--auto` skips HUMAN_REVIEW (current behavior)
- The human review prompt shows: diff, review verdict, audit summary

**State machine becomes:**

```
INIT → WORKSPACE → DEV → VALIDATE → REVIEW → HUMAN_REVIEW → DONE/ESCALATE
                    ↑                  ↓              ↓
                    └──── (REQUEST_CHANGES) ◄─── (reject)
```

**Spec:** `specs/human-in-the-loop.md`

---

### Phase 3: Orchestration Model — Task Decomposition

**Why third:** Once multi-CLI and human-in-the-loop work, the next
bottleneck is that large specs require a single dev agent to do
everything. Paul's pattern is to break work into smaller chunks
manually. The coordinator should do this.

**What it means:**
- New optional pre-DEV phase: `DECOMPOSE`
- A planning agent (Opus-class) reads the spec and produces sub-tasks
- Each sub-task is a mini-spec with its own file_scope
- Sub-tasks run sequentially (not parallel — git worktrees can't merge
  themselves safely)
- Each sub-task goes through the full DEV → VALIDATE → REVIEW cycle
- The coordinator tracks which sub-tasks passed and which didn't
- Final REVIEW covers the full diff, not just the last sub-task

**State machine becomes:**

```
INIT → WORKSPACE → DECOMPOSE → [DEV → VALIDATE → REVIEW]* → HUMAN_REVIEW → DONE
```

**Open questions:**
- Should sub-tasks share a worktree or get their own branches?
- How to handle sub-task dependencies (task B needs task A's output)?
- What if a sub-task's review rejects something that a previous sub-task
  produced?

**Spec:** `specs/task-decomposition.md`

---

### Phase 4: Spec Quality Gate

**Why fourth:** Paul's experience shows that bad specs produce bad code
that burns review cycles. A spec quality check before dev starts saves
time.

**What it means:**
- New optional pre-DEV phase: `SPEC_CHECK`
- A reviewer agent reads the spec and produces findings:
  - Ambiguities
  - Missing acceptance criteria
  - Scope concerns (too large for one agent)
  - Testability gaps
- Findings go to the human (interactive) or fail the run (auto)
- Can be skipped with `--skip-spec-check`

**Spec:** `specs/spec-quality-gate.md`

---

### Phase 5: Live Observability

**Why fifth:** The progress heartbeat is a start, but Paul wants to
know WHAT the agent is doing, not just that it's alive.

**What it means:**
- `forge watch` command that tails the current run
- Shows: current phase, elapsed time, cost so far, last agent action
- For Claude: parse stderr for tool use events
- For Codex/Gemini: parse their respective output formats
- Optional: `forge watch --web` serves a local dashboard

**Spec:** `specs/live-observability.md`

---

### Phase 6: Cross-Project Support

**Why sixth:** TheForge currently works for theforge (dogfooding). Making
it work for hdp or any other project proves generality.

**What it means:**
- Document the forge.yaml schema fully
- Test with a real external project (hdp)
- Handle project-specific quirks:
  - Different test runners (pytest, vitest, jest)
  - Different lint tools (ruff, eslint)
  - Monorepo structures
  - Docker dependencies

**Spec:** `specs/cross-project-support.md`

---

## Testing Strategy

Before building more features, validate what exists:

1. **Dogfood loop:** Write a spec for Phase 1 → run `forge run` on it →
   use the result to implement Phase 1. This is the tightest test.

2. **External project test:** Point forge at hdp with a small spec
   (e.g., "add a utility function") and see what breaks.

3. **Multi-model review test:** Configure `forge.yaml` with a review
   pool and run a real spec. Currently only Claude is supported, so
   the pool would be Claude with different models (opus, sonnet).

4. **Failure mode testing:** Intentionally give a bad spec and verify
   the coordinator escalates correctly. Intentionally break the gate
   and verify retry logic.

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
