# TheForge Restart Meta-Plan — July 2026

**Date:** 2026-07-07
**Status:** Adopted and substantially executed. The wedge work has since shipped
(refusal doctrine #1532, `forge shape` #1541, `forge groom` #1554; repo is at
v0.12.0rc2). Retained as the record of the restart's framing; individual
"parking lot" entries below may have moved since (see notes).
**Provenance:** Drafted after an ~8-week pause (last commit 2026-05-12, `1b56c65`, v0.10.0rc17 + 10 unreleased commits). Based on (a) a full repo/milestone audit and (b) an adversarially-verified research sweep of the Feb–Jul 2026 agentic-SDLC landscape (Claude session), convergent with an independent Codex analysis of the same question.

---

## Mission sentence

> **TheForge turns GitHub issues into safely reviewed, auditable AI-assisted software changes, and refuses work that is not ready.**

TheForge is the **SDLC policy layer**, not an agent runtime. It decides *whether / what / who-reviews / whether-it-merges*. Agent loops, sandboxes, and dispatch infrastructure are commodity and should increasingly be someone else's problem.

This is the grooming filter: work that doesn't serve this sentence gets parked, not resumed.

## Landscape verdict (as of 2026-07-07)

Verified across GitHub Copilot coding agent / Agent HQ / Agentic Workflows, OpenAI Codex cloud, Cognition Devin, Factory.ai, Cursor, Google Jules/Antigravity, Amazon Kiro, Claude Code, OpenHands, and the OSS swarm frameworks:

**Commodity (stop competing here):** issue-to-PR agents (draft-PR terminus), parallel agent fan-out and fleet UIs, worktree/sandbox isolation, event-triggered agent workflows (GitHub Agentic Workflows, public preview 2026-06-11), preference/convention memory, user-driven per-task provider selection.

**Shipped by nobody (the wedge):** backlog-level dependency-DAG orchestration; multi-reviewer quorum review gating merge; first-class refusal/readiness gating (Devin's confidence rating pauses-and-asks; Kiro generates the missing spec rather than refusing); adaptive cross-provider model routing (refuted as commodity); cross-run *evidence* memory over a backlog; per-run dollar budgets (rare: Devin ACU caps, Claude Agent SDK `max_budget_usd`).

**Practitioner evidence validates the thesis:** LinearB 8.1M-PR dataset — high-AI teams merge 98% more PRs with review time up 91%; a May 2026 benchmark found every frontier model reward-hacks (subverts tests to pass broken code). The industry mechanized code production and hit exactly the wall TheForge's gates anticipate.

**Wedge priority:** refusal + diagnosis + audit substrate (ADR-0002). Plan review is already mature; the evidence layer is what makes refusal compound (refusal-economics is unanswerable without it).

**Watch items (re-check quarterly):** Devin (budgets, managed-Devins fan-out) and GitHub Agentic Workflows maturity. Everything else verified as not-competing as of today.

## Milestone sequencing

The 2026-05-03 sequencing **survives intact**: v0.10 visible trust → v0.11 substrate → v0.12 autonomy + failure-evidence → v0.13 adaptive payoff. This plan sharpens what "substrate" means in v0.11: the **audit substrate plus an execution-substrate decision**, not findings cleanup.

---

## Phase 0 — Rehydrate (mechanical, no new features, ~2–3 sessions)

1. **ADR status — verified safe (2026-07-07).** ADR-0001 (Accepted), ADR-0002, and ADR-0003 (proposed) are committed on `main` under `docs/adr/` (#1508/#1514, #1519, #1552). An earlier audit misread them as worktree-only; no rescue needed. This meta-plan doc lands on `main` via PR alongside them.
2. **Reconcile the trunks — at promotion, per RELEASING.md.** `origin/release/v0.10` carries 87 RC-ladder fix commits not on `main`; `origin/main` carries 40 commits of v0.11-substrate work (the three ADRs, SQLite audit substrate Phases B/C incl. reader migration off `history.jsonl` (#1429/#1467/#1465), `forge shape` MVP (#1541), `forge groom` MVP (#1554), compound-engineering doctrine (#1523)). A few late fixes were deliberately double-landed on both branches (#1577, #1578); a merge probe shows ~32 conflict regions. RELEASING.md prescribes forward-porting RC fixes to `main` at promotion; the divergence is by design, just 8 weeks larger than usual. `cut-rc.sh` continues an existing release branch without merging from `main`, so rc18 is not blocked. **Constraint:** do the forward-port before resuming v0.11 sprints against `main`, or dev agents will collide with un-ported fixes. Note: #1434 (review-pool crash escalation) was fixed by PR #1581 on `origin/release/v0.10` on the last active day — drop it from the v0.11 candidate list below if the issue just needs closing.
3. **Size the drift**: `forge check-providers`, refresh the model pool in `forge.yaml` (a generation stale), full `make gate`. Expected breakage: CLI-output parsing in `runners/`, error-string patterns in `provider_health.py`.
4. **Cut rc18.** Ten commits have waited since May 12 (several are `cut-rc.sh` fixes). Resume the ladder; GA timing is paced by dogfood discovery rate, per standing operator rule.
5. **Doc-drift pass**: README badge (says 0.9.0), `docs/vision.md` (says "v0.3", undercounts tests 4×). Run the v0.10 post-release doc review that never happened.

## Phase 1 — Revival slice (~2 weeks)

Run **one real issue** end-to-end through `forge sprint` on the rc18 orchestrator. Candidates: #1576 (sprint summary overwrites outcome history) and #1575 (re-enters merged story after escalation) — floor-relevant coordinator-correctness bugs from the last active day, unlike the P2 findings also parked in v0.10.

Keep a running note of **every point requiring manual judgment**. That list is the v0.11 grooming input. Fix only floor-blockers.

## Phase 2 — Re-milestone (the focus correction)

**Rule: milestones hold intent; findings go to a triaged backlog.**

At pause time, v0.11 had 99 open issues, ~70 of them `forge-finding` P2 `needs-grooming` sediment, including literal duplicates (#1250/#1252, #1219/#1222, #1234/#1238).

- **Bulk-demote the ~70 forge-finding items out of v0.11** (clear milestone, keep labels) after a mechanical dedup pass. Do not hand-groom them — that is the job of the v0.12 triage epics (#1033, #717). Hand-grooming 70 P2s is doing manually what the next milestone exists to automate.
- **v0.11 refocuses to the wedge:**
  - ADR-0002 audit-substrate cluster: #1471, #1453, #1439, #1440, #307, plus audit-fidelity bugs #1422, #1253. Note: a substantial slice already landed on `main` during the final pre-pause week (SQLite index, reader migration, `forge audits` CLI, schema versioning + CI drift guard) — re-audit the cluster against `main` before grooming.
  - Real coordinator-correctness bugs: #1402, #1365, #1546, #1434.
  - Router explainability #1391 (audit/trust-flavored — stays).
  - **New issue: the gh-aw runner spike** (Phase 3).
- **Pull #1532 forward from v0.12** (promote refusal-capability north star to durable doctrine doc) — cheap, and captures this restart's conclusions while fresh.
- **Move the adaptive cluster (#1387, #1389, #1392, #1442) to v0.13**, where the adaptive-payoff theme lives.
- **v0.12 stays as-is** (triage epics, evidence-grounded preflight #1007, integration gate #1006, HITL decision surface #311/#293) and gains a defined purpose: consumer of the demoted findings backlog.

## Phase 3 — GitHub Agentic Workflows spike (an experiment, not a rewrite)

**Rationale:** gh-aw (public preview 2026-06-11) replaces TheForge's highest-maintenance, fastest-rotting layer — `runners/` CLI dispatch and parsing, sandboxing, daemon/status/notification ops, merge plumbing. It replaces **nothing** in the coordinator: no story lifecycle, no quorum reconciliation, no DAG, no gates, no routing, no budgets, no audit substrate. The redrawn architecture: **TheForge decides whether/what/who/when; GitHub executes** — coordinator dispatches via `workflow_dispatch` (engine chosen by the router), collects results via safe outputs/artifacts/check runs, and its gates flip the merge as required status checks.

**Shape:** add `runner_ghaw` as a fifth backend behind the existing runner abstraction. Keep `api.py` as the escape hatch for non-GitHub projects and unhosted engines.

**Success criteria:**
- One story's dev phase executes via gh-aw dispatch.
- Results land in `.forge/audits/` with output-capture fidelity comparable to CLI runners.
- Measured numbers for per-cycle latency (dev→review→dev round-trips) and budget observability (Actions minutes + engine units vs. direct API dollars).

**Output:** ADR-0004 (execution substrate) — proceed / defer / reject, with evidence. gh-aw's preview churn is itself a data point the ADR records.

**Kill criterion:** if per-cycle latency or capture fidelity makes multi-cycle review loops impractical on Actions, the answer is "keep CLI runners, revisit in two quarters" — written down, not re-litigated.

**Sequencing:** does not start until Phase 1 is complete.

## Parking lot (explicitly not resumed now)

- Brief-to-sprint orchestration (vision.md document-in classification)
- Daemon polish and detached-run ops beyond what Phase 0 requires
- New provider adapters
- ~~`forge shape` (#367) and `forge groom` (#1034) epics~~ — **shipped** (MVPs
  landed as #1541 and #1554); no longer parked.

These wait for the wedge. The worst continuation path is resuming every thread as if no time passed.

## Known risks

1. **External-surface drift** — CLI output formats and provider error signatures moved during the idle window; Phase 0 step 3 sizes this before anything else depends on it.
2. **gh-aw preview churn** — building the execution floor on a five-week-old preview trades CLI-parser churn for platform churn; the spike + ADR-0004 is how that trade gets decided on evidence.
3. **Budget semantics on Actions** — per-story dollar enforcement degrades to Actions-minutes + engine-unit accounting; the spike must show whether that's acceptable for `budget_usd`-style governance.
4. **Sediment regrowth** — dogfooding files findings faster than they're triaged. The v0.12 triage epics are the mechanical fix; until then, findings default to the backlog, not milestones.
