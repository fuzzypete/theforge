# ADR-0004: Execution Substrate — GitHub Agentic Workflows (gh-aw) vs CLI Runners

- **Status:** Proposed — recommended verdict: **Defer** (re-entry conditions below); operator decision pending
- **Date:** 2026-07-16 (proposed; live evidence collected same day)
- **Deciders:** Peter Wickersham (project lead); spike executed by Claude, design review by Codex
- **Affected milestones:** v0.11.0 (execution-substrate decision), v0.12.x (runner maintenance consequences)
- **Related issues:** #1680 (spike), PR #1684 (spike implementation)
- **Related ADRs:** ADR-0002 (audit substrate — capture and trust-boundary bar)
- **Related plan:** `docs/plans/restart-meta-plan-2026-07.md` Phase 3

---

## 1. Context and question

TheForge's `runners/` layer — CLI dispatch/parsing, sandboxing, daemon/status ops — is its highest-maintenance, fastest-rotting surface. gh-aw (public preview 2026-06-11; tested at v0.81.6) hosts the same engines inside GitHub Actions with compiled-in sandboxing, network isolation, capture, and safe-output mediation.

**Question:** should GitHub Actions become TheForge's execution substrate — TheForge decides whether/what/who/when; GitHub executes?

**State after the spike:** wiring proven, substrate suitability *partially* proven. One real dev phase executed end-to-end through the production dispatch path on the copilot engine; the claude-engine leg remains unmeasured (blocked on Anthropic API credit balance, not on the substrate).

**Kill criterion (fixed before evidence):** if per-cycle latency or capture fidelity makes multi-cycle review loops impractical on Actions — keep CLI runners, revisit in two quarters, no re-litigation.

## 2. Proposed architecture

- Coordinator keeps: story lifecycle, DAG, gates, quorum review, routing, budget policy, audit substrate. gh-aw replaces nothing here.
- New backend `runner_ghaw` behind the existing runner abstraction: dispatch via `workflow_dispatch` → correlate by dispatch-id in `run-name` → poll → download artifacts → `AgentResult`. Implemented; zero coordinator changes were required, which is the spike's strongest structural result.
- The agentic workflow (`.github/workflows/forge-dev-ghaw.md`, compiled lock file) is **default-branch infrastructure** — `workflow_dispatch` returns HTTP 404 for workflows not on the default branch. It deploys like `forward-port.yml`, not like story code. Per-branch trialing is impossible; the scratch-host-repo pattern substitutes.
- Merge gating as required status checks: designed, not built in the spike.
- **Production follow-up if ever adopted (Codex concurrence):** stop masquerading as `TransportSpec(kind="cli")`; introduce `kind="remote"`. Remote execution has materially different trust, budget, latency, persistence, and cancellation semantics; the spike shape is deliberately low-touch, not the end-state.

### Correlation hazards (convention, not contract)

Run identity rides on a dispatch-id embedded in `run-name`. Known hazards a production design must bound: run-list pagination beyond the poll window, delayed run visibility (measured 13s), duplicate dispatch ids from coordinator retries, run-name truncation, workflow file changing on the default branch mid-poll, and concurrent dispatches of the same workflow. Mitigations: unique id per dispatch (done), artifact echo-back of the id, bounded pagination with a hard ambiguity failure (fail the phase rather than adopt a guessed run).

## 3. Changed ownership boundaries

| Concern | CLI runners (today) | gh-aw substrate |
|---|---|---|
| Sandbox/network isolation | TheForge (`sandbox.py`, seatbelt/bwrap) | gh-aw (container stack: squid + api-proxy + iptables; verified live, per-run firewall audit artifact) |
| Provider credentials | Operator-local env/`.forge/.env` | **GitHub repo secrets, per repo × per engine** (claude→`ANTHROPIC_API_KEY`, copilot→`COPILOT_GITHUB_TOKEN` fine-grained PAT with Account→Copilot Requests: Read; org-billing path does not apply to personal repos) |
| Capture | Runner parses stream-json | Actions artifacts (rendered prompt, agent stdio, patch, usage, firewall audit) |
| Execution compute | Operator's machine | GitHub-hosted runners (public repo: free; private: Actions quota) |
| Failure ops | Local process control | `gh run cancel` + run logs; runaway bounded by `timeout-minutes` and AIC caps |

Bills: engine spend lands on whichever account the engine is keyed to — the operator's Anthropic account (claude engine, API dollars) or the operator's Copilot plan (AI credits, $0.01/credit). Actions minutes land on the repo owner. Model access is subscription-tier-gated on the Copilot channel (premium models rejected with `model_not_supported`; base-tier models only unless the plan covers more).

## 4. Budget model — first-class criterion, not a footnote

Codex review (2026-07-16) is adopted: if TheForge's wedge includes enforceable per-story budget policy, budget governance is a reject/defer criterion in its own right.

Measured reality on gh-aw:
- **No mid-run dollar kill.** The coordinator cannot terminate a run at `budget_usd` the way local runners can.
- **Ceiling enforcement exists:** per-run `max-ai-credits` and a harness daily cap (observed default 1000/run, 5000/day) abort at credit granularity.
- **Post-hoc accounting is exact and per-run:** `agent_usage.json` reports tokens + `ai_credits`; `runner_ghaw` now converts this to `cost_usd` ($0.01/credit), so audit records regain real cost. Measured live: 22.479 AIC = **$0.2248** for one dev phase — within rounding of the same tokens at direct-API prices (no observed reseller markup at base tier).

Net: budget observability is recovered; budget *enforcement* degrades from "kill at dollar X mid-run" to "abort at credit ceiling + reconcile exactly afterward." **The operator must explicitly accept or reject that weakening; latency numbers do not rescue it if rejected.**

## 5. Testing strategy

1. **Lifecycle (in gate):** `tests/fake_bin/gh` subprocess fixture, five failure modes across dispatch→discover→poll→collect, including timeout-cancel and lost dispatch. No `Popen` mocks, per runners conventions.
2. **Contract (env-gated):** pure argv builders validated against installed `gh` grammar (`tests/contract/test_ghaw_cli_contract.py`).
3. **Live substrate (off-gate, costs money):** scratch host repo + driver exercising the real `run_agent` path — the repeatable smoke pattern for workflow changes without touching main.
4. **Not yet done:** full `forge sprint` integration (retry semantics across cold dispatches, review-phase parsing, concurrent stories); capture-fidelity diff harness (same story through `runner_claude` and `runner_ghaw`, field-by-field).

## 6. Operational rollout (if ever adopted)

Prerequisites per target repo: workflow on default branch (lands with PR #1684 for TheForge), engine secrets provisioned (repos × engines matrix — the Copilot PAT collapses this to one account-level token at the cost of tier-gated model access), operator budget-model acceptance (§4), and a `cli: ghaw` profile (explicit opt-in only; no `AGENT_REGISTRY` entry means the router can never auto-select it). Secret rotation is operator-owned (PATs expire; the spike PAT dies 2026-08-15). Artifact retention follows the repo's Actions retention window — audit permanence still requires the coordinator to pull artifacts into `.forge/`, as `runner_ghaw` does.

## 7. Risks

- **Preview churn (realized during the spike, three times in one afternoon):** docs vs binary disagreement on copilot auth; default engine model premium-gated; PR-creation safe output silently degraded to issue+patch. Building on gh-aw trades CLI-parser churn for platform churn — with less recourse, since the platform isn't pinnable the way a CLI version is.
- **Write-path gap:** `create-pull-request` safe output fell back to an issue carrying the patch (run 29528907051 → spike-host issue #3) instead of pushing a branch. TheForge's dev phase contract expects a branch/PR; adoption requires resolving this (permissions/config) or adding a patch-apply step in the coordinator.
- **Prompt transport cap:** `workflow_dispatch` inputs cap at 65,535 chars; real dev prompts can exceed it. Runner fails closed (`PROMPT_TOO_LARGE`); production needs branch- or artifact-based prompt transport, adding a round trip.
- **Cold dispatches:** no session resume — every retry pays full context re-establishment.
- **Agent-quality is not substrate-quality (observed):** the live run executed flawlessly and produced a *wrong* result (measured "470+ tests / 120+ files" vs actual 4,757 / 236). The substrate cannot replace review gates; it feeds them. TheForge's wedge survives substrate choice.

## 8. Decision criteria

Hard gates (any failure → not adopted, regardless of latency):
1. Capture supports review verdicts and audit replay — **met**; artifact capture exceeds local CLI fidelity (adds rendered prompt + network audit).
2. Budget model explicitly accepted by operator — **pending operator decision** (§4).
3. Dev-phase write path produces a reviewable branch/PR — **not met** in spike config (issue+patch fallback).

Soft criteria:
- Per-cycle overhead vs story size: measured **~4.5–5 min fixed overhead per dispatch** (dispatch→visible 13s; activation 15s; agent job 149s of which sandbox+engine setup dominates; detection 47s; safe-outputs 14s; conclusion 38s). A 4-dispatch story pays ~20 min of substrate overhead. Expressed per Codex: acceptable only where total sprint wall-clock and operator intervention stay within tolerance — irrelevant for 20-minute agent jobs, painful for tight review loops.
- Cost parity: base-tier Copilot ≈ direct-API pricing (measured $0.2248/dev-phase).

## 9. Verdict

**Recommended: Defer.** Keep CLI runners primary. Keep `runner_ghaw` as an experimental, explicitly-opted-in backend. Do not route production sprints through gh-aw during preview.

Grounds: hard gate 3 unmet (write path), hard gate 2 unresolved (operator budget acceptance), the claude-engine leg — the engine TheForge would actually route dev to — unmeasured, and three preview-churn incidents in a single afternoon of use. None of this trips the kill criterion (latency and capture are workable), so this is *defer*, not *reject*.

**Re-entry conditions** (any of): gh-aw exits preview or stabilizes its auth/model surface; the PR write path is demonstrated end-to-end; the operator accepts the ceiling-based budget model; story sizes grow to amortize the ~5-min dispatch floor. On re-entry: run the claude-engine leg, the capture-diff harness, and one full sprint; then promote `TransportSpec(kind="remote")` per §2.

## Appendix A — Evidence log (2026-07-16, spike host `fuzzypete/theforge-ghaw-spike`)

| Run | Engine/model | Outcome | Fact established |
|---|---|---|---|
| probe (no run) | — | HTTP 404 | dispatch requires workflow on default branch |
| 29516910990 | copilot (default) | fail @ activation, 25.3s | `COPILOT_GITHUB_TOKEN` required on personal repos; **25s platform floor** with zero agent work |
| 29527906749 | claude | fail @ agent, 259.3s | valid key, API 400 "Credit balance is too low"; sandbox container stack + stream-json capture observed |
| 29528283280 | copilot claude-sonnet-4.6 | fail, 195.7s | default model premium-gated (`model_not_supported`, no retry) |
| 29528669268 | copilot gpt-5-mini | fail, 174.2s | tier gating opaque — included-model lists don't match CLI acceptance |
| 29528907051 | copilot gpt-4.1 | **success, 303.1s** | full path: dispatch 13.0s discovery; agent produced patch + handoff artifact + PR-request (degraded to issue #3); 22.479 AIC = $0.2248; 25,965 in / 1,335 out / 324,352 cached tokens; firewall audit: 30 requests, all `api.githubcopilot.com` |

Job breakdown, successful run: activation 15s → agent 149s → detection 47s → safe-outputs 14s → conclusion 38s (+ queue/gaps ≈ 40s).
