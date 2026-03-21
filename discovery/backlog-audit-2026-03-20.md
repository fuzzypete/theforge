# Backlog Audit — 2026-03-21 (revised)

Comprehensive audit of TheForge docs, backlog, and vision items against
actual code on `main`. Stories now live in `stories/` (not `specs/`).

---

## Section 1: Terminology Audit

### "campaign"

**CLEAN.** Zero references in `src/theforge/`. The `rename-campaign-to-sprint`
story is in `stories/done/`. Vision docs still say "Campaign Mode" in phases
7/9/10 and reference `campaign.py` in Open P2s — those are doc-only fixes.

### "spec" vs "story"

The spec→story rename has **mostly landed**:

| Layer | Status |
|-------|--------|
| CLI help text | **story** — `forge run` says "story", `forge sprint` says "story" |
| Code internals | **TaskStory** preferred, `TaskSpec` is backward-compat alias via `spec_validator.py` shim |
| Directory structure | **stories/** — canonical path, `specs/` symlink removed |
| forge.yaml | `stories:` list in sprint manifests |
| Agent prompts (task.py) | Mostly "story" |
| Ideate output | One remnant: prints "spec" path on ideate output |

**Remaining work:** `spec-to-story-rename.md` is in backlog — covers the last
remnants (ideate output path, any straggling references). Low priority.

### "file_scope"

**CLEAN.** Zero references in `src/theforge/`. Fully removed from code.
`drop-file-scope.md` should move to done/.

### CLAUDE.md

**Mostly current.** Shows correct state machine (PLAN + PLAN_REVIEW), says
"story" is primary term, `TaskSpec` is backward-compat alias. Still references
`specs/` paths in a few places — minor.

### Other doc issues

- `docs/vision.md` phases 7/9/10 still say "campaign" and "spec"
- `docs/vision.md` §Open P2s references `campaign.py` (now `sprint.py`)
- `getting-started.md` clone URL may reference wrong GitHub org
- `docs-terminology-consistency.md` story exists in backlog to address all of this

---

## Section 2: Vision Items — Status

### Roadmap Phases (docs/vision.md)

| Phase | Name | Status |
|-------|------|--------|
| 1 | Multi-CLI Support | **SHIPPED** |
| 2 | Human-in-the-Loop | **SHIPPED** |
| 3 | Multi-Model Review Pool | **SHIPPED** |
| 4 | Preflight Validation | **SHIPPED** |
| 5 | Live Activity Stream | **SHIPPED** |
| 6 | Auto-Merge | **SHIPPED** |
| 7 | Sprint Mode | **SHIPPED** |
| 8 | Cross-Project Support | **PARTIAL** — forge.yaml documented, `forge init` exists, no external project validation suite |
| 9 | Dependency Analysis | **NOT STARTED** — `dependency-analysis.md` in backlog but no code. Needs rethink now that file_scope is gone |
| 10 | Parallel Sprint Execution | **NOT STARTED** — depends on Phase 9 |
| 11 | Upstream Orchestration | **PARTIAL** — `forge ideate` exists, `--dev-model` exists, document classifier and `--until`/`--through` do not |

### Enhancement Queue (docs/vision.md)

| Item | Status |
|------|--------|
| Graceful timeout warning | **SHIPPED** — 80% nudge in API mode |
| Timeout auto-tuning | **NOT STARTED** |
| Resume after timeout | **SHIPPED** |
| Merge logic duplication | **SHIPPED** — extracted to `coord_workspace.py` |
| Gate command complexity | **PARTIAL** — output captured, no step-level diagnostics |
| Worktree bootstrapping | **SHIPPED** — `workspace.setup_command` |
| Large spec handling | **NOT STARTED** |
| Multi-model dev fallback | **SHIPPED** — smart_config_models escalation |
| Multi-model ideation | **SHIPPED** — `ideate.py` 3-phase deliberation |

### Agent Intelligence (docs/vision/agent-intelligence.md)

| # | Item | Status |
|---|------|--------|
| 1 | PLAN failure blocks | **SHIPPED** |
| 2 | Progress-aware timeouts | **NOT STARTED** — story written in backlog |
| 3 | Task decomposition | **NOT STARTED** |
| 4 | Source code analysis | **NOT STARTED** |
| 5 | Timeout-triggered escalation | **NOT STARTED** — story written in backlog (`timeout-model-escalation.md`) |
| 6 | Plan review before DEV | **SHIPPED** — PLAN_REVIEW phase with multi-model pool |

### Cost-Tiered Generation (docs/vision/cost-tiered-generation.md)

| Item | Status |
|------|--------|
| Structured plan output | **STORY WRITTEN** — `structured-plan-output.md` in backlog. No code |
| Plan review gate | **SHIPPED** |
| Plan validation (mechanical) | **SHIPPED** — `plan_validator.py` with `validate_plan()`. Advisory, never blocks DEV |
| Local model adapter | **PARTIAL** — Ollama routing via OpenAI-compat exists (`--dev-model ollama/...`). No Aider/Cline/OpenCode adapter. Story written: `local-model-adapter.md` |
| Phase telemetry | **PARTIAL** — `forge telemetry` command exists (`print_phase_cost_duration_telemetry`). Story in backlog may want more |

---

## Section 3: Discovery Doc Sprint Plan — Status

### Sprint 1: Observability

| Story | Status |
|-------|--------|
| `agent-trace-artifacts` | **SHIPPED** — `traces.py`, `write_trace()` throughout coordinator |

### Sprint 2: Story-first workflow

| Story | Status |
|-------|--------|
| `lean-story-output` | **SHIPPED** |
| `pipeline-entry-points` | **PARTIAL** — `--plan` flag exists. `--until <phase>` does NOT exist. `stage-aware-pipeline.md` written in backlog as the successor |
| `vision-pipeline-flexibility` | **PARTIAL** — vision.md updated with story terminology but still has stale campaign/spec language |

### Sprint 3: HITL optimization

| Story | Status |
|-------|--------|
| `human-review-brief` | **NOT STARTED** — story in backlog |
| `decision-surface` | **NOT STARTED** — story + epic in backlog |
| AI-assisted decision options | **NOT STARTED** — no story written |

### Big rename

| Item | Status |
|------|--------|
| campaign→sprint | **SHIPPED** — done/ story exists, zero code references |
| spec→story | **MOSTLY SHIPPED** — CLI, code, directories all use "story". One ideate remnant. `spec-to-story-rename.md` in backlog for cleanup |
| file_scope removal | **SHIPPED** — zero code references |

---

## Section 4: Backlog Story Audit

### SHIPPED — should move to done/

| Story | Evidence |
|-------|----------|
| `drop-file-scope.md` | Zero `file_scope` references in src/theforge/ |
| `ollama-openai-compat-routing.md` | Ollama provider normalization in cli.py + config.py |
| `p2-bug-fixes.md` | Commit 3e792a4 "P2 bug fixes — cost tracking, plan parsing, audit data shape" |
| `cache-aware-cost-estimation.md` | Commit f8daa04 + `cache_read` pricing in runner_api.py and runner.py |
| `p1-line-enforcement.md` | `schemas.py:66` — P1 with file must cite specific line |
| `review-convergence.md` | `finding_classifier.py` — sha256 fingerprinting + Jaccard matching for multi-cycle convergence |

**6 stories should move to done/.**

### ACTIVE — real work remaining

#### Coordinator & Architecture

| Story | Notes |
|-------|-------|
| `coordinator-refactor.md` | coordinator.py still 2,405 lines (target <800). Phase 2 cleanup |
| `split-coordinator-tests.md` | test_coordinator.py still monolithic |
| `coord-loop-refactor-regression-tests.md` | Regression test coverage |
| `runtime-artifacts-under-forge.md` | handoff.yaml still at worktree root, not `.forge/` |

#### Pipeline & Workflow

| Story | Notes |
|-------|-------|
| `pipeline-entry-points.md` | Superseded by `stage-aware-pipeline.md` — keep one, archive the other |
| `stage-aware-pipeline.md` | `--until` and `--from` flags. No code exists |
| `workspace-branch-collision.md` | Handle existing branches gracefully |
| `stale-approve-triage.md` | Fix false-positive ALREADY_DONE on abandoned runs |

#### Decision Surface & HITL

| Story | Notes |
|-------|-------|
| `decision-surface.md` | Generalized decision gates — not started |
| `human-review-brief.md` | AI decision summary in notifications — not started |
| `escalate-hitl-gate.md` | Human decision before terminal failure — not started |

#### Model & Escalation

| Story | Notes |
|-------|-------|
| `dev-model-escalation.md` | Timeout-triggered escalation gap remains |
| `timeout-model-escalation.md` | Exit=-9 triggers model escalation — not started |
| `progress-aware-timeouts.md` | Stuck detection via tool-call rate — not started |
| `adaptive-model-assignment.md` | History-based model selection — not started |
| `escalation-learning.md` | Learn from escalation patterns — not started |

#### Review & Quality

| Story | Notes |
|-------|-------|
| `review-pool-resilience.md` | Per-reviewer parse retry and degradation hardening |
| `commit-centric-review-prompt.md` | Review prompt fully driven by git log + git show |
| `pr-review-attribution.md` | Per-reviewer GitHub reviews (post_run hook exists, native integration doesn't) |
| `api-loop-diagnostics.md` | Per-turn tool call logging for API agents |

#### Plan Maturity

| Story | Notes |
|-------|-------|
| `structured-plan-output.md` | YAML step-by-step format — prerequisite for cost-tiered generation |
| `dev-observability-and-anchoring.md` | Dev traces and handoff in audit trail |

#### Intelligent Defaults

| Story | Notes |
|-------|-------|
| `smart-defaults.md` | Opinionated forge.yaml generation — not started |
| `run-health-metrics.md` | Per-run scoring and anomaly detection — not started |
| `phase-telemetry.md` | May be partially shipped (`forge telemetry` exists) — verify scope |

#### Documentation (new epic)

| Story | Notes |
|-------|-------|
| `docs-readme-restructure.md` | Landing funnel structure |
| `docs-terminology-consistency.md` | Audit terminology across all docs |
| `docs-hello-forge-golden-path.md` | Self-contained golden path example |
| `docs-troubleshooting-guide.md` | Symptoms/fixes troubleshooting |
| `docs-first-run-walkthrough.md` | Narrated terminal transcript |
| `docs-cli-use-this-when.md` | Opinionated CLI guidance |
| `docs-runtime-artifacts.md` | Filesystem layout documentation |
| `docs-resume-semantics.md` | Resume behavior state matrix |
| `docs-provider-setup-chooser.md` | Provider setup decision guide |

#### Future / Larger Scope

| Story | Notes |
|-------|-------|
| `dependency-analysis.md` | Needs rethink — was file_scope-based, file_scope is gone |
| `dev-scope-escalation.md` | SCOPE_BLOCKED verdict — not started |
| `local-model-adapter.md` | Aider/Cline/OpenCode CLI adapters (Ollama-via-OpenAI exists) |
| `forge-daemon.md` | Persistent background sprint runner |
| `github-native-integration.md` | First-class GitHub PR/issue support (beyond hooks) |
| `release-automation.md` | Changelog and GitHub Releases |
| `gemini-adapter-hardening.md` | Gemini CLI adapter robustness |

### STALE — should archive or merge

| Story | Rationale |
|-------|-----------|
| `pipeline-entry-points.md` | Superseded by `stage-aware-pipeline.md` — archive one |
| `spec-format-docs.md` | Overlaps with `docs-terminology-consistency.md` and `docs-hello-forge-golden-path.md` |
| `dependency-analysis.md` | file_scope-based design is obsolete. Needs rewrite, not execution |
| `backlog.md` | Meta-file with notes, not an executable story |
| `audit-improvements.md` | Vague scope — verify if remaining items are covered by other stories |

### IN PROGRESS

| Story | Location |
|-------|----------|
| `spec-validation.md` | `stories/active/` — story_validator.py implemented, being developed |

---

## Section 5: Missing Stories

Items discussed in vision/discovery/memory with no backlog story.

| Source | Gap | Description |
|--------|-----|-------------|
| vision.md Phase 8 | `cross-project-validation` | Test forge on a real external project |
| vision.md Phase 10 | `parallel-sprint-execution` | Concurrent worktrees within sprint batches |
| vision.md Phase 11 | `document-classifier` | Structural detection to auto-route doc types |
| vision.md Phase 11 | `forge-plan-sprint` | Sprint planning from dependency graph |
| agent-intelligence §3 | `task-decomposition` | PLAN signals DECOMPOSE_NEEDED, coordinator chains sub-stories |
| agent-intelligence §4 | `source-code-health` | `forge audit --health` with file metrics |
| discovery Sprint 3 | `ai-decision-options` | Opus-generated options at every decision gate |
| vision.md Enhancement | `timeout-auto-tuning` | Track durations, recommend timeout changes |

**8 gaps.** Down from 17 in the first audit — many were written as stories
since then (structured-plan-output, plan-validation, timeout-model-escalation,
progress-aware-timeouts, phase-telemetry, stage-aware-pipeline,
local-model-adapter, commit-centric-review-prompt).

---

## Section 6: Proposed Epic Structure

### Epic 1: Backlog Hygiene (immediate)
Move 6 shipped stories to done/. Archive `pipeline-entry-points` (superseded).
Merge or archive overlapping stories. Verify `phase-telemetry` scope.

### Epic 2: Pipeline Flexibility
`stage-aware-pipeline` (--until/--from), `document-classifier`.
Unlocks the iterative plan→review→dev workflow.

### Epic 3: Decision Surface
`decision-surface`, `human-review-brief`, `escalate-hitl-gate`,
`ai-decision-options` (needs story). Epic doc exists.

### Epic 4: Plan Maturity & Cost-Tiered Generation
`structured-plan-output`, `local-model-adapter`.
Plan validation already shipped. Prerequisite chain for cheap DEV pass 1.

### Epic 5: Intelligent Defaults
`smart-defaults`, `run-health-metrics`, `adaptive-model-assignment`,
`escalation-learning`. Epic doc exists.

### Epic 6: Coordinator Cleanup
`coordinator-refactor` Phase 2, `split-coordinator-tests`,
`coord-loop-refactor-regression-tests`, `runtime-artifacts-under-forge`.
coordinator.py is 2,405 lines (target <800).

### Epic 7: Agent Intelligence
`timeout-model-escalation`, `progress-aware-timeouts`,
`task-decomposition` (needs story), `source-code-health` (needs story).

### Epic 8: Review Quality
`commit-centric-review-prompt`, `review-pool-resilience`,
`api-loop-diagnostics`, `pr-review-attribution`.
Review convergence already shipped. Epic doc exists.

### Epic 9: Sprint Parallelism (future)
`dependency-analysis` (rewrite needed), `parallel-sprint-execution` (needs story).
Blocked on rethinking dependency mechanism without file_scope.

### Epic 10: Documentation
11 `docs-*` stories covering readme, golden path, troubleshooting, CLI guide,
resume semantics, provider setup, runtime artifacts, terminology, diagrams,
cross-linking, first-run walkthrough. Epic doc exists.

### Epic 11: Operational Polish
`workspace-branch-collision`, `stale-approve-triage`, `gemini-adapter-hardening`,
`release-automation`, `dev-scope-escalation`, `spec-to-story-rename` (remnants).

### Epic 12: Platform Vision (future)
`forge-daemon`, `github-native-integration`, `cross-project-validation`.
Larger scope items for when core stabilizes.

---

## Summary Counts

| Category | Count |
|----------|-------|
| Backlog stories total | ~62 (including docs stories) |
| Should move to done/ | 6 |
| Active (real work) | ~48 |
| Stale/archive | 5 |
| In progress | 1 (spec-validation) |
| Done stories | 68 |
| Missing stories (gaps) | 8 |
| Proposed epics | 12 |

---

## Changes from First Audit (2026-03-20)

Major shifts since the original audit was written:

1. **spec→story rename landed** — directories, CLI, code all use "story"
2. **file_scope fully removed** — zero code references
3. **campaign fully removed** — zero code references
4. **23 shipped stories already moved to done/** — backlog is now clean
5. **17 "missing" stories reduced to 8** — many were written as backlog stories
6. **New modules exist**: `finding_classifier.py` (review convergence),
   `plan_validator.py` (mechanical plan checks), `story_validator.py`
   (pre-PLAN quality), `spec_validator.py` (backward-compat shim)
7. **Ollama support shipped** via OpenAI-compatible routing
8. **11 new docs stories** written as a docs-improvement epic
9. **Cache-aware cost estimation shipped**
10. **P1 line enforcement shipped**
