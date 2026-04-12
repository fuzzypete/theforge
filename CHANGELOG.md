# Changelog

All notable changes to TheForge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]


## [0.6.0] — 2026-04-12

### Added

- **Sibling-worktree write detector:** parallel CLI workers that write to the same source files are detected and rejected before the collision corrupts both branches (#625, #638)
- **Conventions check runs with gate:** convention violations (line length, circular imports, module size) are checked in parallel with the gate rather than as a post-gate afterthought — failures surface earlier (#611)
- **Identical-failure circuit breaker:** gate retries are cut short when the same failure repeats verbatim, preventing wasted dev iterations on a deterministically broken gate (#598)

### Fixed

- **Landing status is now authoritative:** `CoordinatorResult.success` reflects merge/push outcome separately from review approval — "approved but failed to land" is a distinct, visible outcome in the audit trail (#600)
- **Merge-pr is resumable and fail-closed:** `_merge_pr` is broken into discrete steps with state checkpointing; pending auto-merge polls are bounded and fail closed on timeout rather than hanging the sprint indefinitely (#607, #632, #633)
- **Landing serialized in scheduler thread:** parallel workers no longer race to merge — the scheduler thread holds the integration lock, preventing interleaved squash-merges from corrupting the base branch (#626)
- **Dependent stories no longer skip on `pending_integration`:** stories waiting on a dependency that was queued for auto-merge were incorrectly classified as permanently blocked; they now wait correctly and unblock when the dependency lands (#642)
- **Workspace fails closed on diverged base branch:** when `git pull --ff-only` fails because local and origin have diverged, the sprint aborts with a clear error instead of creating a worktree from stale local state (#661)
- **Preflight evaluates clean baseline, not stale worktree:** `ALREADY_DONE` verdicts are assessed against the current base branch, not a previously-created worktree that may be behind origin (#588)
- **Invalid preflight and handoff are hard stops:** a non-`PROCEED` preflight verdict and an invalid dev handoff (after retries) both halt the story rather than normalizing to proceed-anyway behavior (#597, #596)
- **Dev agent scratch files caught by write detector:** throwaway exploration files created outside standard project directories during dev are flagged and blocked from landing on main (#650)
- **Re-exec after `git pull`:** when the sprint runner pulls new forge source code at startup, the process re-execs so the updated code is actually running before work begins (#646)
- **CLI launcher sandbox wrapping removed:** CLI runners (Claude, Codex) were being sandbox-wrapped despite `forge.yaml` explicitly disabling sandbox — removed the erroneous wrapping (#599, #624)
- **Reviewer demotion counter resets between review cycles:** a reviewer demoted in cycle 1 was permanently demoted for the run; demotion state now resets at the start of each new review cycle (#610)
- **Audit iteration counts match actual invocations:** dev and review iteration counts in the audit trail now reflect actual agent calls rather than retry-loop bookkeeping values (#601)
- **Sandbox availability recorded in state and audit:** whether the sandbox is available at run start is captured in coordinator state and surfaced in the audit trail (#604)
- **Duplicate findings deduplicated in review merge:** identical findings from multiple reviewers in the same pool are collapsed before being surfaced or carried forward (#605)
- **Multi-line acceptance-criteria bullets parsed correctly:** `_extract_story_acceptance_criteria` was truncating wrapped markdown bullets at the first newline, causing valid dev handoffs to fail story-consistency checks (#666)

### Changed

- **Auto-model escalation frozen behind feature flag:** automatic model tier escalation on repeated failures is disabled by default; enable via `forge.yaml` when the behavior has been validated (#609)
- **Net-new P1 bypass frozen behind feature flag:** the rule allowing a net-new P1 finding to bypass escalation is disabled by default (#608)

### Refactored

- **`RetryBudget` consolidates retry counters:** scattered dev/review retry tracking replaced with a single `RetryBudget` object with uniform accounting (#603)
- **`retry_reason` replaced with `Enum`:** string-based retry reason replaced with a typed `Enum` throughout the coordinator (#602)
- **Subprocess-based project code isolation:** `prepend_worktree_src` path manipulation replaced with a subprocess wrapper that sets the correct `PYTHONPATH` per invocation, eliminating `sys.path` leakage between parallel workers (#606)


## [0.5.0] — 2026-04-07

### Added

- **`forge sprint --issues N,M,...`:** run a specific list of issues without a label or milestone manifest (#415)
- **`--base-branch` override:** target a release or feature branch per-run; `{base_branch}` substitution in `workspace.create_command` eliminates the need for per-branch config files (#534)
- **Immediate parallel merge landing:** stories merge as they complete in parallel sprints via an integration lock — no more waiting for the slowest worker (#526)
- **Commit-centric review:** reviewer reads `git log` + `git show` for the worktree branch rather than file diffs, matching the PR-review mental model (#429)
- **Native GitHub integration:** first-class PR, issue, and review support in the sprint runner (#427)
- **Provider fallback:** automatic CLI → API fallback for the same provider (e.g. Codex CLI → OpenAI API) when the CLI is unavailable (#402)
- **Cascading P1 escalation:** a new P1 in the same file/function as a prior P1 is treated as persistent, not novel (#401)
- **Unresolved P1s carried forward:** surviving P1 findings are injected verbatim into the next dev prompt (#397)
- **Preflight collision detection:** preflight emits `likely_files`; the sprint runner uses these to detect parallel-worker file conflicts before they happen (#413)
- **Per-run reviewer demotion:** a reviewer that repeatedly fails to produce parseable output is demoted for the remainder of that run (#426)
- **`forge stop` waits by default:** blocks until the daemon exits rather than returning immediately (#515)
- **Parallel log prefixing:** `forge logs` prefixes each line with the story slug in parallel mode (#433)
- **CI verification between merges:** sprint runner checks that main CI stays green before landing the next story (#460)
- **`{forge_python}` substitution:** single source of truth for the Python interpreter in forge commands (#474)
- **`scripts/release.sh`:** scripted release process — milestone check, gate, CHANGELOG, tag, release branch, dev bump, GitHub release (#533)

### Fixed

- **Handoff integrity:** coordinator verifies dev self-reported handoffs against actual worktree changes; supplements missing fields (#528)
- **Sprint resume duplicate PRs:** resume no longer opens a second PR for stories already merged (#468)
- **Daemonize race:** double-fork race condition caused a fresh sprint launch to conflict with itself (#476)
- **Dev retry without verification:** dev retry now requires actual worktree changes, not just a self-reported handoff (#450)
- **Parallel merge race:** base branch modified mid-sprint no longer causes merge-pr to target a stale ref (#399)
- **Sprint worker exceptions swallowed:** worker failures now surface as ESCALATE with a full error in the audit trail (#393)
- **Resumed stories skip collision detection:** resumed stories now re-register file footprints before re-entering the plan phase (#457)
- **Gate timeout orphans:** killing the gate process group on timeout cleans up pytest worker processes (#530, #525)
- **Worktree resume hygiene:** config is synced from repo root and stale plan files are removed on resume (#522)
- **Sprint run.log coherence:** run.log is truncated on restart rather than appended — each run produces a single coherent record (#452)
- **merge-pr branch protection:** merge-pr uses `--auto` flag to respect branch protection rules (#449)
- **Preflight invalid verdict passthrough:** preflight returning a non-PROCEED verdict no longer silently falls through with empty `likely_files` (#447)
- **Stale lock cleanup hardening:** lock files store the owning PID; stale locks from dead processes are cleared on next sprint resume (#519)
- **Rebase resumed worktrees:** resumed worktrees are rebased onto the current base branch before dev begins (#517)
- **Rerun workspace setup on reused worktrees:** workspace setup is repeated when a worktree is reused, preventing stale state (#516)
- **Merge fallback detection via audit trail:** sprint triage detects squash-merge fallbacks from the audit trail rather than branch presence (#514)
- **Duplicate PR guard uses `mergedAt`:** coordinator uses `mergedAt` (not `merged`) to guard against duplicate PRs after auto-merge (#513)
- **Preflight API profile matching:** preflight now matches API profiles in the model registry correctly (#512)
- **Queued auto-merge treated as pending:** a PR with queued auto-merge is no longer counted as merged when scheduling dependents (#511)
- **`--until plan` flag enforcement:** `forge run --until plan` now stops at the correct phase boundary (#520)
- **Parallel sys.path leakage:** parallel sprint workers no longer inherit the project root on `sys.path`, preventing import collisions (#451)
- **Sprint triage false-positive SKIP MERGED:** triage no longer marks an empty or stale worktree branch as already merged (#409, #404)
- **`.forge/` artifacts scrubbed from branch history:** handoff and plan files are de-indexed before merge so they never appear in main's history (#470)
- **Concurrent sprint guard:** a second `forge sprint` launch against an active worktree is rejected immediately (#469)
- **Sprint run.log coherence in parallel mode:** stderr tee race in parallel workers is fixed (#431)
- **Plan reviewer failure surfaced:** empty output from a plan reviewer agent is now recorded as a finding in the audit, not silently ignored (#430)
- **Adapter schema finalization scoped correctly:** review-schema finalization is skipped for preflight and dev phases (#455)
- **Gemini finalization with `response_schema`:** Gemini no longer applies `response_schema` during plain-text ideation runs (#390)
- **Missing and stale fields in sprint/story audit output** (#529)

## [0.4.1] — 2026-04-04

### Fixed

- **Gemini thought_signature capture:** read from `Part` (not `FunctionCall`, which has no such field) — standard models were silently dropping the signature, causing 400 INVALID_ARGUMENT on every iter 2+ call (#381)
- **Gemini thought_signature replay:** `thought_signature` is now a sibling of `function_call` on the Part dict, matching the SDK schema (#381)
- **Plan reviewer pool:** restored DeepSeek-reasoner as first plan reviewer (was accidentally duplicated as Gemini twice); bumped Gemini plan reviewer `max_iterations` from 10 → 20 (#387)
- **Stale lock file on SIGTERM:** lock files now contain the owning PID; stale locks from dead processes are detected and cleared on next sprint resume (#370)
- **Interactive dev workflow:** documented issue → worktree → commit → handoff sequence in CLAUDE.md and AGENTS.md

## [0.4.0] — 2026-04-04

### Added

- **Gemini thinking mode:** `thinking_budget` config field on Gemini API
  profiles enables extended thinking; thinking tokens counted in cost
  estimation; `forge check-config` reflects the setting (#252)
- **Paperless sprints:** `forge sprint` pulls stories directly from GitHub
  issues via milestone or label query — no local manifest files required (#253)
- **Sprint query mode:** deferred merge behavior for GH-sourced sprints (#364)
- **on_approve: merge-pr:** auto-merge approved branches with PR audit trail (#159)
- **Story source abstraction:** specs from GitHub issues, local files, or
  external trackers (#165)

### Changed

- **Plan review corroboration:** single-reviewer P1 findings downgraded to
  reduce false-positive churn (#249)
- **Plan regen trajectory focus:** dominant theme filtering for more coherent
  plan regeneration (#250)

### Fixed

- **Gemini adapter hardening:** handle empty responses, thought_signature
  errors, and blocked content gracefully (#251)
- **AC-violation gate:** net_new_pass no longer overrides P1s that violate
  acceptance criteria (#278)
- **Sprint pre-pull race:** pull base branch once before parallel workers to
  avoid ref contention (#470)

## [0.3.0] — 2026-04-01

### Added

- **Config diagnostics:** `forge check-config` shows the effective config,
  auth readiness, and startup warnings before a run
- **Operational commands:** `forge status`, `forge logs`, `forge stop`, and
  `forge decide` for monitoring detached runs and resolving pending decisions
- **Telemetry reporting:** `forge telemetry` summarizes per-phase cost and
  duration across historical runs
- **Run controls:** `forge run` now supports `--until`, `--from`,
  `--reviewers`, `--max-cycles`, `--fg`, and `--no-pull`
- **Preflight classification upgrades:** work-type classification and
  spec-sufficiency assessment inform planning depth and review behavior
- **Conventions support:** soft conventions are prompt-injected and hard
  conventions can enforce line counts, test mirroring, and circular-import rules
- **Sprint safety:** story-level concurrency guards and stricter dependency
  handling prevent conflicting runs
- **Editable-install version suffixes:** `forge version` appends `-dev+g<hash>`
  when an editable checkout is ahead of the latest tag

### Changed

- **Package layout:** large modules were split into focused packages:
  `cli/`, `config/`, `coordinator/`, `runners/`, `sprint/`, and `task/`
- **Story scaffolding:** `forge init` now creates `stories/TEMPLATE.md`
- **Ideation output:** `forge ideate` now defaults to `stories/<slug>.md`
- **Secrets path:** `.forge/.env` is the canonical secrets file; legacy
  `.forge/secrets.yaml` should be migrated
- **Local model configuration:** OpenAI-compatible local servers are configured
  with `provider: openai` and `base_url`

### Fixed

- **Resume/worktree hygiene:** better stale-state triage and root-config sync
- **Plan review reliability:** improved regeneration prompts, trace retention,
  and failure auditing
- **Review severity scoping:** P1 findings are scoped to code changed by the run
- **Provider/config validation:** stronger auth checks, normalization, and
  startup warnings

## [0.1.0] — 2026-03-19

### Added

- **Core pipeline:** INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV →
  VALIDATE → REVIEW → DONE/ESCALATE
- **Multi-CLI agents:** Claude Code, Codex CLI, and Gemini CLI as subprocess
  agents with real-time activity streaming
- **API-mode agents:** OpenAI, Anthropic, Google, and DeepSeek via HTTP with
  TheForge-managed tool runtime (Read, Edit, Write, Bash, Glob, Grep)
- **Multi-model review pool:** Fan-out to N independent reviewers with
  deterministic synthesis reconciliation. A single P1 from any reviewer triggers
  REQUEST_CHANGES.
- **Plan phase:** Planning agent produces implementation plan before dev starts;
  optional multi-model plan review pool catches structural issues early
- **Preflight phase:** One-shot spec classification (PROCEED/ALREADY_DONE/BLOCKED)
  before expensive dev+review cycles
- **Sprint mode:** `forge sprint manifest.yaml` runs multiple stories
  sequentially with shared budget
- **Multi-LLM ideation:** `forge ideate` for collaborative spec generation via
  multi-model deliberation protocol
- **Provider smoke test:** `forge check-providers` verifies connectivity and
  auth for all configured providers
- **Budget enforcement:** Per-profile cumulative cost ceilings with token-level
  cost tracking for API-mode agents
- **Schema-enforced review output:** Cross-validation catches APPROVE+P1 and
  REQUEST_CHANGES+no-P1 contradictions
- **Stale worktree detection:** `forge run --resume` triages existing worktrees
  and resumes from the correct phase
- **Agent loop features:** Iteration-based nudge at 80%, time-based nudge at 80%
  of wall-clock deadline, forced finalization on timeout
- **Audit trail:** Per-run audit YAML with timing, per-agent cost breakdown,
  model usage detail, review findings, and gate decisions
- **Structured logging:** JSONL event stream with phase-level timing
- **Per-run verbose log:** Stderr tee to `.forge/logs/` for post-mortem debugging
- **Auto-merge:** `--auto-merge` flag merges approved feature branches to main
- **Notifications:** ntfy and osascript backends for completion alerts
- **Local model support:** API-mode profiles support `base_url` for Ollama,
  LM Studio, vLLM, and other OpenAI-compatible servers
- **Dotenv secrets:** Project-scoped secrets in `.forge/.env` (gitignored)

### Providers

| Provider | Mode | Models |
|----------|------|--------|
| Anthropic | CLI + API | Claude Sonnet, Claude Opus, Claude Haiku |
| OpenAI | CLI + API | GPT-5.4, o4-mini, Codex |
| Google | CLI + API | Gemini 2.5 Pro, Gemini 2.5 Flash |
| DeepSeek | API | DeepSeek V3, DeepSeek R1 |

### Compatibility

- Python 3.11, 3.12, 3.13
- macOS, Linux (Windows not tested)
