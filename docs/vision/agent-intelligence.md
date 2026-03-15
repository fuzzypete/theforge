# Vision: Agent Intelligence & Autonomous Decision-Making

Captured 2026-03-15 from discussion on coordinator refactor failure modes.

## Context

The coordinator refactor (2,900-line coordinator.py + 7,500-line test file)
exposed several gaps between how forge operates and how a skilled human
would manage the same work.

---

## 1. PLAN Failure Should Block, Not Fail-Open

**Current:** PLAN times out → "proceeding to DEV without plan" → dev agent
flies blind → burns budget → fails.

**Should be:** PLAN failure → ESCALATE with decision surface:
- Retry with more time
- Simplify/decompose the spec
- Escalate to human

If the task was complex enough to need a plan, sending a dev agent without
one is just burning money.

## 2. Interrupt vs Kill — Progress-Aware Timeouts

**Current:** Hard SIGKILL at N seconds. Zero information about what the agent
was doing or whether it was making progress.

**How a human works:** Look at what the agent is doing, assess whether it's
making progress or stuck, either give more time or redirect.

**Possible approaches:**
- **Progress heuristics** — track tool calls per minute. 40 calls in 5 min =
  working. 2 calls in 5 min = stuck. Use this to distinguish "needs more
  time" from "spinning."
- **Soft timeout / hard timeout** — at soft timeout, surface a decision via
  ntfy: "Agent running 10 min, 47 tool calls, last action: reading
  test_coordinator.py line 4000. Continue / Kill / Extend?"
- **Progress checkpoints** — analyze heartbeat/tool-call patterns before
  deciding to kill. A monotonically increasing read-offset pattern =
  systematic progress. Repeated reads of the same file = stuck.

**Constraint:** CLI agents (claude, codex, gemini) don't support mid-run
interruption. Any "interrupt" mechanism would be at the coordinator level
(kill + preserve partial work) rather than agent level.

## 3. Task Decomposition

**Insight:** If PLAN looks at a spec and determines it's too big for one
agent pass, it should decompose it into ordered sub-tasks that run
sequentially.

**Example:** The coordinator refactor spec already has ordered steps:
1. Create coord_state.py, move dataclasses
2. Create coord_notify.py, move notify functions
3. Create coord_gate.py, move gate functions
4. ... etc.

A smart PLAN phase could turn each step into a sub-spec and run them as
a chain. Each sub-task is small enough for sonnet in 10 minutes.

**Architecture implications:**
- Coordinator needs to support chained sub-tasks (spec → plan → N sub-specs)
- Each sub-spec inherits the worktree from the previous one
- Gate runs after each sub-spec (fail-fast)
- Review runs once at the end (or after each — configurable)

**Open question:** Does decomposition belong in PLAN or in a separate
DECOMPOSE phase? PLAN produces a plan for a single task. DECOMPOSE
decides whether the task IS a single task.

## 4. Proactive Source Code Analysis

**Insight:** The coordinator refactor need was obvious for a while —
coordinator.py was growing with every sprint, merge conflicts were
increasing, test files were getting unwieldy. A human notices this
pattern; forge currently doesn't.

**Possible approach:** A periodic (or pre-sprint) analysis step that:
- Measures file sizes, function counts, cyclomatic complexity
- Tracks growth rate across recent commits
- Identifies files crossing thresholds (e.g., >1000 lines, >30 functions)
- Flags "this file has grown 40% in the last 10 commits"
- Suggests refactor specs automatically

**Fits theforge?** Could be a `forge audit --health` or `forge analyze`
command. Runs static analysis, produces a report with refactor
recommendations. Could even generate spec drafts.

**Prior art:** SonarQube, CodeClimate, but those are external services.
This would be lightweight, integrated, and oriented toward generating
actionable forge specs rather than dashboard metrics.

## 5. Timeout-Triggered Model Escalation

**Current:** `smart_config_models` escalation (sonnet → codex → opus) only
fires when `_has_persistent_p1()` detects the same P1 finding across
consecutive review cycles. If the dev agent *times out* (exit=-9, SIGKILL),
the coordinator retries with the same model at the same timeout — guaranteed
to fail again for tasks that are genuinely too large.

**What happened:** Coordinator refactor — sonnet timed out 3x at 10 minutes
each, producing 450k chars of output per attempt. It was working (not stuck),
but the task was too big for sonnet's context + speed. The coordinator never
escalated to codex or opus because it never reached REVIEW.

**Should be:** Dev timeout (exit=-9) should trigger model escalation the same
way a persistent P1 does. If sonnet times out, try codex. If codex times
out, try opus. The escalation signal is "this model can't finish in time"
rather than "this model's output has bugs."

**Implementation sketch:**
- In the coordinator loop, after a dev timeout (exit_code == -9 or
  elapsed >= timeout), check `smart_config_models` and escalate before
  retrying.
- Possibly increase timeout proportionally when escalating (opus gets more
  time than sonnet — it's slower but more capable).
- Log clearly: "Dev agent timed out (600s) — escalating from sonnet to codex"

**Relationship to complexity adaptation:** `_apply_complexity_adaptation()`
already adjusts timeouts based on preflight complexity (large → 2x timeout).
Timeout-triggered escalation is the reactive complement: complexity adaptation
is proactive (predict), timeout escalation is reactive (observe and adapt).

---

## 6. Plan Review Before Dev (Critical Gap)

**Current implementation:** PLAN → DEV. Plan is trusted as-is, unreviewed.

**How the project owner actually worked:**
1. Opus produces plan document
2. Codex + Gemini review the *plan* (not the code — the plan itself)
3. Human reads synthesized plan review, may push back or adjust
4. Only after approval does dev execute

**Why this matters:** Plan review is cheaper than code review. A wrong plan
caught before DEV costs one synthesis call. The same wrong plan caught at
REVIEW after 3 dev cycles costs 3× dev + 3× review + potential escalation.

**What the review looks for is different from code review:**
- Gaps in the plan (missing edge cases, unhandled states)
- Wrong assumptions about the codebase (Gemini's `_run_shell` catch was valid
  but unverified — a code-grounded reviewer would confirm or refute it)
- Ordering problems (step 3 depends on step 5)
- Circular import risks
- Missing test scenarios

**Target state:** `PLAN → PLAN_REVIEW(pool) → HUMAN_REVIEW(ntfy) → DEV`

The human gets an ntfy with the synthesized plan + reviewer notes and can:
- Approve → DEV starts with the plan
- Request changes → PLAN reruns with feedback
- Reject / redirect → ESCALATE

**forge.yaml config:** A `plan_review` profile (lightweight — reviewers don't
need to execute code, just reason about the plan document). Could reuse the
existing review pool with a different prompt.

**Relationship to current HUMAN_REVIEW:** This is a new decision gate, not
a replacement. HUMAN_REVIEW fires on code review APPROVE. PLAN_REVIEW fires
before any code is written. Both feed into the decision surface vision.

---

## Priority for Implementation

1. **PLAN failure blocks** — small change, high impact, ✅ done
2. **Timeout bumps** — config change, ✅ done
3. **Plan review before DEV** — high impact, corrects core workflow gap, spec it next
4. **Timeout-triggered model escalation** — medium effort, high impact, spec it
5. **Progress-aware timeouts** — medium effort, high value, spec it
6. **Task decomposition** — large effort, transformative, needs ideation
7. **Source code analysis** — independent track, good forge-ideate candidate
