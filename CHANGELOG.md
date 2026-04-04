# Changelog

All notable changes to TheForge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
