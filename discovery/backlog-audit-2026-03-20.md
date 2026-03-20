# Backlog Audit — 2026-03-20

Comprehensive audit of TheForge docs, backlog, and vision items against
actual code on `main` as of commit `2264e09`.

---

## Section 1: Terminology Audit

### "campaign" in active files

| Location | Context | Action needed |
|----------|---------|---------------|
| `specs/backlog/rename-campaign-to-sprint.md` | The spec itself | Close — production code has zero "campaign" references |
| `specs/backlog/notifications.md` | References "campaign/run notifications" | Update wording to "sprint" |
| `specs/backlog/dependency-analysis.md` | References "campaign manifest" | Update wording to "sprint" |
| `tests/test_sprint.py` | 25 occurrences (class names, test names) | Rename when convenient — tests only |
| `tests/test_coord_state.py` | 4 occurrences in test names | Same — tests only |
| `tests/test_coordinator.py` | 3 occurrences in comments | Same — tests only |
| `docs/vision.md` Phase 7, 9, 10 | "Campaign Mode", "campaign manifest", "campaign runner" | Update to "sprint" |

**Production `src/theforge/` has zero "campaign" references.** The rename shipped
in code but was never cleaned up in docs, specs, and tests.

### "spec" vs "story" in user-facing contexts

The `spec-to-story-rename` commit (c2504e8) was **reverted** (2264e09). "Spec" is
the canonical term in code. However, vision.md and the discovery doc use "story"
as the user-facing term. This creates a split:

| Layer | Term used | Examples |
|-------|-----------|---------|
| Vision docs | "story" | `docs/vision.md` §Upstream Workflow, §Current State |
| Discovery doc | "story" | Throughout — "story-first workflow" |
| CLI help/prompts | "spec" | `forge run <spec-file>`, prompt text in `task.py` |
| Code internals | `TaskSpec` | `task.py`, `cli.py`, `sprint.py` |
| forge.yaml | "specs" | `specs:` list in sprint manifests |
| Inputs reference | Mixed | `docs/guides/inputs-reference.md` uses "Story file" |

**Decision needed:** Either commit to the rename (spec→story in CLI/prompts/docs)
or standardize on "spec" everywhere. Current state is inconsistent.

### Other terminology issues

- `docs/vision.md` Phase 7 still says "Campaign Mode" — should be "Sprint Mode"
- `docs/vision.md` Phase 9 says "Spec Dependency Analysis" — should be "Story"
  if following the terminology table, or "Spec" if keeping current
- `docs/vision.md` §Open P2s references `campaign.py` — file is now `sprint.py`
- `forge.yaml` comment says "per-spec audit" — matches code but not vision docs

---

## Section 2: Vision Items — Status

### Roadmap Phases (docs/vision.md)

| Phase | Name | Status |
|-------|------|--------|
| 1 | Multi-CLI Support | **SHIPPED** — `runner.py` dispatches to claude/codex/gemini |
| 2 | Human-in-the-Loop | **SHIPPED** — `--interactive`, HUMAN_REVIEW phase |
| 3 | Multi-Model Review Pool | **SHIPPED** — fan-out + synthesis in `runner.py` |
| 4 | Preflight Spec Validation | **SHIPPED** — `coord_preflight.py` |
| 5 | Live Activity Stream | **SHIPPED** — stream-json parsing in `runner.py` |
| 6 | Auto-Merge | **SHIPPED** — `coord_workspace.py` |
| 7 | Campaign/Sprint Mode | **SHIPPED** — `sprint.py` |
| 8 | Cross-Project Support | **PARTIAL** — forge.yaml documented, `forge init` exists, no external project test suite |
| 9 | Spec Dependency Analysis | **NOT STARTED** — no graph coloring or batch grouping code exists |
| 10 | Parallel Campaign Execution | **NOT STARTED** — depends on Phase 9 |
| 11 | Upstream Orchestration | **PARTIAL** — `forge ideate` exists, document classifier does not, `--through` flag does not exist |

### Enhancement Queue (docs/vision.md)

| Item | Status |
|------|--------|
| Graceful timeout warning | **SHIPPED** — 80% time-based nudge in API mode (`runner_api.py`) |
| Timeout auto-tuning | **NOT STARTED** |
| Resume after timeout | **SHIPPED** — `session-resume.md` in done/ |
| Merge logic 3x duplication | **SHIPPED** — extracted to `coord_workspace.py` |
| Frontmatter parsing duplication | **STALE** — `campaign.py` renamed to `sprint.py`, check if still duplicated |
| Double manifest load | **STALE** — needs recheck against current `sprint.py` |
| Missing failed-merge campaign test | **STALE** — terminology outdated, may still be valid |
| Gate command complexity | **PARTIAL** — gate output captured, but no step-level diagnostics |
| Worktree bootstrapping | **SHIPPED** — `workspace.setup_command` exists in `config.py` |
| Large spec handling | **NOT STARTED** — no automatic decomposition |
| Multi-model dev fallback | **SHIPPED** — `smart_config_models` escalation in coordinator |
| Multi-model ideation | **SHIPPED** — `ideate.py` with 3-phase deliberation protocol |

### Agent Intelligence Items (docs/vision/agent-intelligence.md)

| # | Item | Status |
|---|------|--------|
| 1 | PLAN failure blocks | **SHIPPED** — noted as done in the doc itself |
| 2 | Progress-aware timeouts | **NOT STARTED** — no tool-call rate tracking |
| 3 | Task decomposition | **NOT STARTED** — no DECOMPOSE phase or sub-spec chaining |
| 4 | Proactive source code analysis | **NOT STARTED** — no `forge audit --health` command |
| 5 | Timeout-triggered model escalation | **PARTIAL** — dev-model-escalation spec exists, persistent P1 triggers escalation, but timeout (exit=-9) does NOT trigger escalation |
| 6 | Plan review before DEV | **SHIPPED** — `PLAN_REVIEW` phase with multi-model pool (`plan-review-pool.md` in done/) |

### Cost-Tiered Generation (docs/vision/cost-tiered-generation.md)

| Item | Status |
|------|--------|
| Structured plan output (YAML steps) | **NOT STARTED** — plan output is freeform markdown |
| Plan review gate | **SHIPPED** — `PLAN_REVIEW` phase exists |
| Plan validation (mechanical) | **NOT STARTED** — no structural validation of plan content |
| Aider adapter | **NOT STARTED** — no `_run_aider()` in runner.py |
| CLI adapter for Cline/OpenCode | **NOT STARTED** |
| Trial & telemetry | **NOT STARTED** |

---

## Section 3: Discovery Doc Sprint Plan — Status

### Sprint 1: Observability

| Story | Status |
|-------|--------|
| `agent-trace-artifacts` | **SHIPPED** — `traces.py`, `write_trace()` throughout coordinator. Also in done/ as `trace-capture.md` |

### Sprint 2: Story-first workflow

| Story | Status |
|-------|--------|
| `lean-story-output` | **SHIPPED** — `specs/done/lean-story-output.md` |
| `pipeline-entry-points` | **PARTIAL** — `--plan` flag exists in CLI. `--until <phase>` does NOT exist |
| `vision-pipeline-flexibility` | **PARTIAL** — vision.md updated with story terminology and upstream workflow, but still contains stale terminology (campaign, spec) |

### Sprint 3: HITL optimization

| Story | Status |
|-------|--------|
| `human-review-brief` | **NOT STARTED** — spec in backlog, no AI-generated decision summary in notifications |
| `decision-surface` | **NOT STARTED** — spec in backlog, no generalized decision gate framework |
| AI-assisted decision options | **NOT STARTED** — no spec written |

### Big rename

| Item | Status |
|------|--------|
| campaign→sprint in code | **SHIPPED** — production code uses "sprint" exclusively |
| campaign→sprint in tests/docs | **NOT DONE** — 32 test references, several doc references remain |
| spec→story rename | **REVERTED** — commit c2504e8 reverted by 2264e09 |

---

## Section 4: Backlog Spec Audit

### SHIPPED — should move to done/

| Spec | Evidence |
|------|----------|
| `agent-trace-artifacts.md` | `traces.py` exists, `write_trace()` in coordinator. Duplicate of done/`trace-capture.md` |
| `targeted-fix-prompt.md` | `build_handoff_fix_prompt()` in `task.py`. Smart-escalation epic marks it done |
| `structured-dev-handoff.md` | `devhandoff.py` with `DevHandoff` dataclass, `parse_dev_handoff()` |
| `plan-agent-review.md` | `PLAN_REVIEW` phase, `plan_agent_review` config section. Duplicate of done/`plan-review-pool.md` |
| `plan-injection.md` | `--plan` flag in `cli.py:1185` |
| `extract-coord-state.md` | `coord_state.py` exists with Phase enum + dataclasses |
| `extract-coord-notify.md` | `coord_notify.py` exists |
| `extract-coord-gate.md` | `coord_gate.py` exists |
| `extract-coord-preflight.md` | `coord_preflight.py` exists |
| `extract-coord-workspace.md` | `coord_workspace.py` exists |
| `sprint-dependencies.md` | `depends_on` parsing + enforcement in `sprint.py` |
| `dotenv-secrets.md` | `.env` loading in `cli.py` + `config.py`. Duplicate of done/`project-secrets.md` |
| `deepseek-provider.md` | DeepSeek in `config.py` MODEL_REGISTRY + `runner_api.py` |
| `lifecycle-hooks.md` | `coord_hooks.py` with `run_hook()`, `post_run` in forge.yaml |
| `pr-on-approve.md` | `on_approve: pr` in config, `gh pr create` in `coord_phases.py` |
| `findings-gh-hook.md` | `post_run.sh` hook, `forge init-hooks` command |
| `api-mode-dev.md` | `runner_api.py` with full tool runtime. Duplicate of done/`api-agent-loop.md` |
| `stale-worktree.md` | `_is_stale_worktree()` in `coord_workspace.py` |
| `project-local-logs.md` | `coord_logging.py`, `.forge/logs/forge.log` |
| `run-log-capture.md` | Structured JSONL logging via `StructuredLogger` |
| `provider-smoke-test.md` | `forge check-providers` command in `cli.py` |
| `auto-push.md` | `auto_push` config in `config.py`, git push in `coord_workspace.py` |
| `pre-validate-command.md` | `pre_validate` exists in `coord_phases.py` + `config.py` |

**23 specs should move to done/ or be deleted as duplicates.**

### ACTIVE — real work remaining

| Spec | Status | Notes |
|------|--------|-------|
| `coordinator-refactor.md` | ACTIVE | Parent epic. 5 extraction specs shipped but coordinator.py still ~1900 lines. Phase 2 cleanup remains |
| `split-coordinator-tests.md` | ACTIVE | `test_coordinator.py` still monolithic |
| `budget-enforcement.md` | ACTIVE | Sprint-level enforcement exists, but per-profile cumulative ceiling (mid-run halt) may not be wired |
| `parallel-review.md` | STALE | ThreadPoolExecutor already in `runner.py` for review pool. Check if spec asks for more |
| `review-from-commit.md` | ACTIVE | Review prompt references `git log main..HEAD` but may not use compact changed-files manifest |
| `pipeline-entry-points.md` | ACTIVE | `--plan` exists but `--until <phase>` does not |
| `human-review-brief.md` | ACTIVE | No AI-generated decision summary in ntfy notifications |
| `decision-surface.md` | ACTIVE | No generalized decision gate framework |
| `dev-model-escalation.md` | ACTIVE | Persistent P1 escalation works, but timeout-triggered escalation does not |
| `dev-scope-escalation.md` | ACTIVE | No SCOPE_BLOCKED verdict in code |
| `auto-conflict-resolution.md` | ACTIVE | Merge conflict resolution exists in `coord_workspace.py` but may not match spec fully |
| `session-resume-mainline-salvage.md` | ACTIVE | Codex resume path may not be wired |
| `coord-loop-refactor-regression-tests.md` | ACTIVE | Regression test coverage for coordinator semantics |
| `runtime-artifacts-under-forge.md` | ACTIVE | `handoff.yaml` still at project root, not `.forge/handoff.yaml` |
| `plan-review.md` | STALE | Spec describes HITL gate between plan and dev. `PLAN_REVIEW` exists as agent review. Human plan review may differ |
| `smart-defaults.md` | ACTIVE | `forge init` exists but no intelligent config generation |
| `run-health-metrics.md` | ACTIVE | No per-run health scoring or metrics dashboard |
| `adaptive-model-assignment.md` | ACTIVE | No historical performance-based model selection |
| `escalation-learning.md` | ACTIVE | No learning from escalation patterns |
| `p1-line-enforcement.md` | ACTIVE | Need to verify if P1 findings require line numbers |
| `pr-review-attribution.md` | ACTIVE | Per-reviewer GitHub reviews with branch protection |
| `spec-format-docs.md` | ACTIVE | Story format documentation updates |
| `gemini-adapter-hardening.md` | ACTIVE | Gemini CLI adapter exists but may need hardening |
| `release-automation.md` | ACTIVE | No release automation exists |
| `review-pool-resilience.md` | ACTIVE | Degraded mode exists but spec may ask for more |
| `audit-improvements.md` | ACTIVE | May have remaining items beyond current audit capabilities |

### STALE — should archive or close

| Spec | Rationale |
|------|-----------|
| `rename-campaign-to-sprint.md` | Production code already uses "sprint". Only tests/docs remain — this is cleanup, not a feature spec |
| `drop-file-scope.md` | `file_scope` still in `task.py`, `coordinator.py`, etc (7 files). Work not done, but memory says "going away" — decision needed on whether to pursue |
| `parallel-review.md` | Review pool already uses ThreadPoolExecutor. If spec asks for more, reclassify as ACTIVE |
| `notifications.md` | ntfy backend shipped, osascript shipped. Check if spec asks for email/script backends |
| `backlog.md` | Meta-file with P2 notes, not an executable spec |

### DUPLICATE — covered by done/ specs

| Backlog spec | Duplicate of (in done/) |
|-------------|------------------------|
| `agent-trace-artifacts.md` | `trace-capture.md` |
| `plan-agent-review.md` | `plan-review-pool.md` |
| `dotenv-secrets.md` | `project-secrets.md` |
| `api-mode-dev.md` | `api-agent-loop.md` |

---

## Section 5: Missing Stories

Work items discussed in vision docs, discovery docs, or memory files with
**no corresponding spec in backlog**.

| Source | Missing story | Description |
|--------|---------------|-------------|
| vision.md Phase 8 | `cross-project-support` | External project test harness + documentation |
| vision.md Phase 9 | `dependency-graph-builder` | `dependency-analysis.md` exists but may need update for graph-coloring approach |
| vision.md Phase 10 | `parallel-sprint-execution` | Concurrent worktrees within a sprint batch |
| vision.md Phase 11 | `document-classifier` | Structural detection to auto-route briefs/stories/sprints |
| vision.md Phase 11 | `stage-aware-pipeline` | `--enter` and `--through` flags for pipeline flexibility |
| vision.md Phase 11 | `forge-plan-sprint` | Sprint planning from dependency graph |
| vision.md Enhancement | `timeout-auto-tuning` | Track actual durations, surface recommendations |
| agent-intelligence.md §2 | `progress-aware-timeouts` | Tool-call rate tracking for stuck detection |
| agent-intelligence.md §3 | `task-decomposition` | PLAN signals DECOMPOSE_NEEDED, coordinator chains sub-specs |
| agent-intelligence.md §4 | `source-code-health` | `forge audit --health` with file metrics and refactor suggestions |
| agent-intelligence.md §5 | `timeout-model-escalation` | Exit=-9 triggers model escalation (distinct from P1 escalation) |
| cost-tiered.md Phase 0 | `structured-plan-output` | YAML step-by-step plan format with spec-requirement mapping |
| cost-tiered.md Phase 0 | `plan-validation` | Mechanical plan structure check (coverage, valid paths) |
| cost-tiered.md Phase 1 | `aider-adapter` | `_run_aider()` in runner.py for local/cheap models |
| cost-tiered.md Phase 3 | `phase-telemetry` | Per-phase model performance tracking |
| discovery §5 Sprint 3 | `ai-decision-options` | Context-aware Opus-generated options at every decision gate |
| discovery §6 | `sprint-parallelism` | DAG-aware batch scheduling with failure propagation |
| memory: churn root cause | (shipped) | `cycle-aware-feedback` and `trace-capture` both in done/ |
| memory: commit-centric | `commit-centric-review-prompt` | Review prompt fully driven by `git log` + `git show` |
| memory: drop file_scope | `remove-file-scope` | Strip `file_scope` from TaskSpec schema entirely |

---

## Section 6: Proposed Epic Structure

### Epic 1: Backlog Hygiene
Move 23 shipped specs to done/. Archive 4 duplicates. Update stale terminology
in 3 specs. Close `rename-campaign-to-sprint.md` (code done, tests/docs are cleanup).

### Epic 2: Pipeline Flexibility
`pipeline-entry-points` (--until), `stage-aware-pipeline` (--enter/--through),
`document-classifier`. Enables the iterative plan-then-dev workflow from the
discovery doc.

### Epic 3: Decision Surface
`decision-surface`, `human-review-brief`, `ai-decision-options`. Generalized
HITL decision gates with AI-generated context at every decision point.

### Epic 4: Cost-Tiered Generation
`structured-plan-output`, `plan-validation`, `aider-adapter`, `phase-telemetry`.
Cheap models for DEV pass 1 when plan is strong. Prerequisite: plan maturity.

### Epic 5: Intelligent Defaults
`smart-defaults`, `run-health-metrics`, `adaptive-model-assignment`,
`escalation-learning`. Self-tuning from blank config. Already has an epic doc.

### Epic 6: Coordinator Cleanup
`coordinator-refactor` Phase 2, `split-coordinator-tests`,
`coord-loop-refactor-regression-tests`, `runtime-artifacts-under-forge`.
Reduce coordinator.py to <800 lines, split test file.

### Epic 7: Agent Intelligence
`progress-aware-timeouts`, `timeout-model-escalation`, `task-decomposition`,
`source-code-health`. Making the coordinator observe and adapt like a human would.

### Epic 8: Sprint Parallelism
`dependency-graph-builder` (update existing `dependency-analysis.md`),
`parallel-sprint-execution`, `sprint-parallelism`. Phases 9-10 of the roadmap.

### Epic 9: Upstream Orchestration
`document-classifier`, `forge-plan-sprint`, `remove-file-scope`,
`commit-centric-review-prompt`. Phase 11 territory — brief to sprint plan.

### Epic 10: Operational Polish
`pr-review-attribution`, `gemini-adapter-hardening`, `release-automation`,
`review-pool-resilience`, `spec-format-docs`, `dev-scope-escalation`.
Independent improvements that can ship anytime.

---

## Summary Counts

| Category | Count |
|----------|-------|
| Backlog specs total | 55 |
| Should move to done/ | 23 |
| Active (real work) | ~24 |
| Stale/close | 4 |
| Duplicate of done/ | 4 |
| Missing stories (gaps) | ~17 |
| Proposed epics | 10 |
