# ADR-0004: Execution Substrate — GitHub Agentic Workflows (gh-aw) vs CLI Runners

- **Status:** Proposed (evidence collection in progress — see "Pending evidence")
- **Date:** 2026-07-16 (proposed)
- **Deciders:** Peter Wickersham (project lead)
- **Affected milestones:** v0.11.0 (execution-substrate decision), v0.12.x (consequences for runner maintenance load)
- **Related issues:** #1680 (gh-aw runner spike)
- **Related ADRs:** ADR-0002 (audit substrate — the capture-fidelity bar this substrate must clear)
- **Related plan:** `docs/plans/restart-meta-plan-2026-07.md` Phase 3

---

## Context

TheForge's `runners/` layer — CLI dispatch and parsing, sandboxing, daemon/status ops, merge plumbing — is the highest-maintenance, fastest-rotting part of the system: every provider CLI release is a potential contract break (see the CLI-contract test layer born from #1011). GitHub Agentic Workflows (gh-aw, public preview 2026-06-11) hosts the same engines (Copilot, Claude, Codex, Gemini) inside GitHub Actions with compiled-in sandboxing, network isolation, log capture, and safe-output mediation.

The question this ADR decides: **should GitHub Actions become TheForge's execution substrate?** The redrawn architecture under evaluation: TheForge decides whether/what/who/when; GitHub executes. The coordinator dispatches via `workflow_dispatch`, collects results via artifacts/safe outputs/check runs, and its gates flip the merge as required status checks. gh-aw replaces nothing in the coordinator — no story lifecycle, quorum reconciliation, DAG, gates, routing, budgets, or audit substrate.

The spike (#1680) added `runner_ghaw` as a fifth backend behind the existing runner abstraction (`TransportSpec(kind="cli", runner="ghaw", executable="gh")` — dispatch, run correlation by dispatch-id in `run-name`, polling, artifact collection into `AgentResult`). `api.py` remains the escape hatch for non-GitHub projects and unhosted engines.

## Kill criterion (fixed before evidence, per the spike issue)

If per-cycle latency or capture fidelity makes multi-cycle review loops impractical on Actions, the answer is **keep CLI runners, revisit in two quarters** — written down here, not re-litigated.

## Evidence

### Collected (2026-07-16, gh-aw v0.81.6, spike host `fuzzypete/theforge-ghaw-spike`)

1. **Dispatch workflows are default-branch infrastructure.** `workflow_dispatch` resolves workflows against the repository's default branch only; dispatching `forge-dev-ghaw.lock.yml` on a feature branch returns HTTP 404 (`workflow ... not found on the default branch`). Consequence: the gh-aw dev workflow must be merged to `main` as standing infrastructure (like `forward-port.yml`) before the coordinator can use it, and cannot be trialed per-story-branch. The spike worked around this with a scratch host repo whose default branch carries the workflow.

2. **Engine auth is a per-repo secrets prerequisite.** The copilot engine on a personal repo hard-requires the `COPILOT_GITHUB_TOKEN` secret (run 29516910990: activation job failed secret validation; the documented no-PAT `copilot-requests` billing path did not apply). The claude engine requires `ANTHROPIC_API_KEY` as a repo secret. Substrate consequence: provider credentials move from operator-local env/keychain into GitHub repo secrets — a trust-boundary change ADR-0002 consumers should note.

3. **Platform overhead floor ≈ 25s per dispatch before any agent work.** Measured through `runner_ghaw` end-to-end: dispatch → run visible in `gh run list` = 13.4s; dispatch → run concluded = 25.3s for a run that executed only the activation job (queue + runner provision + gh-aw activation). Every dev→review→dev round-trip pays this floor per dispatched phase, plus checkout/engine-install time in the agent job (not yet measured — see pending).

4. **Prompt transport is capped at 65,535 characters.** `workflow_dispatch` inputs carry the dev prompt; TheForge dev prompts (story + conventions + handoff history) can exceed this. `runner_ghaw` fails closed (`PROMPT_TOO_LARGE`) rather than truncating. Production use would need artifact- or branch-based prompt transport, which adds a round-trip to the overhead floor.

5. **Budget semantics degrade as predicted** (meta-plan risk 3, now structural fact): the transport reports `cost_usd=None`; spend is observable only as Actions minutes + engine units (`gh aw logs`/`audit`, AIC caps like `GH_AW_MAX_DAILY_AI_CREDITS`). Per-story `budget_usd` enforcement — mid-run kill on dollar overrun — has no mechanism on this substrate; only per-run AIC ceilings exist.

6. **Preview churn is real but the toolchain held.** Docs and installed v0.81.6 disagreed on copilot auth (documented org-billing path vs. actual hard secret requirement). `gh aw compile` worked first-pass; one workflow compiles to ~106 KB of generated lock-file YAML that must be committed and recompiled on every gh-aw upgrade — a new generated-artifact churn surface in the repo.

### Pending evidence (blocked on operator setting the engine secret)

- One story's dev phase executing end-to-end (story: #1634 doc-drift slice) — agent-job duration, and therefore realistic per-cycle latency.
- Output-capture fidelity of the run's artifacts vs. `runner_claude`'s stream-json capture (`.forge/traces/` parity).
- Engine-unit / Actions-minute consumption for one dev phase vs. the equivalent direct-API dollar cost.

## Verdict

**Withheld until the pending evidence lands.** The structural findings so far cut both ways and none alone trips the kill criterion:

- Against: 25s+ per-dispatch floor (multi-cycle loops pay it 4–8×/story), budget enforcement degraded to ceilings, prompt-transport cap, secrets and default-branch prerequisites as new operator surface.
- For: the sandbox/capture/ops layer TheForge maintains by hand today is compiled in; the runner abstraction absorbed the new backend in one module + four config touchpoints, with no coordinator changes — confirming the "replace runners only" architecture is real.

The verdict section will be completed with a proceed / defer / reject decision when the live-run numbers exist. If per-cycle latency lands above what multi-cycle review loops tolerate, the kill criterion applies as written.

## Consequences (of running the spike, independent of verdict)

- `runner_ghaw` exists behind the abstraction (`cli: ghaw` profiles), gated by the same conventions as other runners (fake-CLI lifecycle tests, `gh` contract tests). It is spike-grade: no session resume, no API fallback, no model-usage parsing.
- `.github/workflows/forge-dev-ghaw.{md,lock.yml}` and `.github/aw/` are committed; the lock file is marked `linguist-generated`.
- The scratch host repo `fuzzypete/theforge-ghaw-spike` (private) holds the live-run evidence and can be deleted once this ADR is Accepted/Rejected.
