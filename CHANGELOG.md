# Changelog

All notable changes to TheForge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- v0.11.0 development (main) -->
### Changed

- **Assignment history is now a derived view (#793):** completed stories no
  longer rewrite `.forge/assignment_history.yaml`. Adaptive routing reads
  escalation history straight from the SQLite audit substrate, so unrelated
  parallel branches no longer collide on that snapshot. Any checked-in
  `.forge/assignment_history.yaml` can be deleted post-upgrade — the audit
  substrate already contains the same facts. Run
  `forge audits export-assignment-history` to rebuild a local human-readable
  snapshot on demand. `forge init`/`forge secrets-init` now ignore the file
  in fresh repositories by default.

### Fixed

- **A reuse gate that ran out of time now says so to the dev agent (#2796):**
  sprint resume runs a gate on an existing worktree and routes the story to DEV
  when it does not pass, but a gate killed at its `validation.gate_timeout`
  budget arrived as an ordinary first-iteration prompt — no mention that a gate
  had run at all — so the agent searched for a failing test in a suite where
  nothing was failing. The timeout now travels as structured state from triage
  into the resumed run (`forge sprint --resume` and `forge run --resume` alike),
  and the first dev prompt states that the gate did not finish, names the
  configured budget and the measured elapsed time, and says explicitly that no
  test failed. The run audit records the same values under
  `workspace.entry_gate`, with `workspace.entry_gate_surfaced_to_dev` recording
  whether the agent was actually told. Audit record schema version 43.

- **Failing-test extraction no longer silently assumes pytest (#1738):** the
  gate-failure retry path extracted failing-test identifiers using pytest's
  summary grammar only, so a project whose gate speaks another toolchain
  (xcodebuild, `make`, etc.) got an empty failed-test list on every failure —
  indistinguishable from a genuine no-test-failure gate error, and with no
  signal that the retry was running degraded. Extraction now (a) reports
  whether it recognized the gate's output format, surfacing an explicit note in
  the dev retry, a `⚠` log line, a `failed_test_extraction_skipped` audit event,
  and a `gate_output_format_recognized` field on dev-iteration telemetry when it
  did not apply; and (b) honors a new optional `validation.failed_test_pattern`
  regex so a project can declare how its gate names failures and point retries
  at the exact failing tests. Pytest projects are unaffected.

<!-- v0.10.0 content forward-ported from release/v0.10; promote-rc.sh renames
     the release branch's section at promotion — reconcile then. -->

The headline of v0.10.0 is **workflow determinism and operator trust**. v0.9.0
introduced adaptive intelligence; v0.10.0 makes the work that adaptive
intelligence drives observable, reproducible, and trustworthy. Sprint state
surfaces no longer disagree with each other. Audit truth is preserved through
redaction. Failure modes that previously stranded work — null-byte PR bodies,
silent dependency loss, fragmented profile history, contaminated escalation
state — now surface or self-recover. Issue intake is structured (issue type and
fix-readiness as first-class inputs, automatic remediation of shape gaps before
stories are dropped) so the dev pipeline runs against signal, not guesses. A
new `forge diagnose` flow separates root-cause discovery from fix work for
symptom bugs. Models gain canonical identity and a user-declared registry
overlay so adding a model no longer requires a TheForge release.

This release continues the **dogfood-from-releases** discipline: floor-blocker
fixes for substrate quality (state coherence across audit/summary/status,
preflight verification on reopened stories, reviewer AC enforcement) landed to
make v0.10.0 safe to build v0.11.0 on.

### Added

- **The complexity pause carries a decomposition assessment (#2686):** when the
  preflight complexity gate opens, it no longer shows only the question. One
  bounded, read-only agent invocation now produces an assessment from evidence
  preflight already holds — candidate slices, each with a title and a scope
  boundary; the dependency edges between them; how the original story's
  acceptance criteria distribute across them; and the decisions it could not
  settle. It is rendered on the pause and carried as data on the pending record
  (`preflight_complexity_gate.assessment`), so answering the question is a review
  of something concrete rather than an investigation. **It mutates nothing** —
  the invocation is sealed to a read-only tool surface with no shell, a
  read-only sandbox, and inference credentials only, and it runs against a clean
  baseline checkout, so no issue is created, edited, or closed and the original
  story stays intact and runnable whichever way the pause is answered (applying
  an assessment is #2824). A story that is genuinely atomic despite a high score
  — and every failure path: an agent that could not launch, one that returned
  failure, output that failed validation — emits the pause with its question and
  a recorded statement that no assessment was produced and why; the absence never
  blocks the pause from being answered. A split that would drop an acceptance
  criterion, or that names a dependency on a slice it did not declare, is
  refused by the parser rather than shown. The assessment and the operator's
  disposition of it (`assessment_disposition`) are written to the run audit so
  assessment quality can later be measured against whether a split that was
  acted on landed, and its cost is recorded per run — bounded to half the
  configured planning budget and to a fraction of the pause's own wait window,
  so the step stays cheap relative to the planning spend it exists to displace.
- **Preflight complexity gate (#2681):** a story whose preflight verdict is
  PROCEED and whose complexity score reaches
  `retry.preflight_complexity_gate_threshold` (shipped: 9, active by default)
  now pauses at the end of PREFLIGHT and asks the operator to approve it as
  scoped (`forge decide <story-run-id> approve`) or return it for decomposition
  (`… decompose`) — before planning, dev, or review is charged. Approving
  continues exactly as before and records the approval at that score; returning
  it ends the run having spent only preflight and reports the story as
  *returned for decomposition* (`outcome: decomposed`), not as a failure. Other
  stories in the sprint keep running while one waits. An expired gate takes
  `retry.preflight_complexity_gate_no_decision`, which accepts only the same two
  actions — anything absent, empty, or unrecognised returns the story, so no
  misconfiguration can spend on an unapproved one. Raising the threshold above
  10 disables the gate; there is no separate enable switch. Every PROCEED score
  at or above the threshold opens it, including one derived by a degraded
  preflight or by one that examined no criteria. Such a score is conservatively
  *high* precisely because the story could not be sized, which is a reason to
  ask rather than to skip asking — so the provenance is shown to the operator
  with the score and recorded as
  `preflight_complexity_gate.score_provenance_note` on the audit, as context for
  the decision rather than a suppression of it.
- **`forge diagnose` flow:** a separate flow for root-cause discovery on symptom
  bugs, distinct from fix work. Operators can run diagnosis as its own bounded
  step before deciding whether to sprint a fix. (#1154)
- **Issue type as first-class structured input:** sprint contract reads issue
  type from a structured field instead of inferring it from prose, so dev
  agents reason against the operator's intent rather than rephrasing it. (#1142)
- **Fix-readiness signal as structured intake input:** symptom bugs, diagnosed
  bugs, and fix-ready bugs are distinguishable in the intake contract so
  preflight and plan-review can route them differently. (#1153)
- **Intake remediation gate:** sprint intake auto-fixes shape and grooming
  failures (or proposes a comment-mode fix) before dropping stories. The model
  shifts from "groom by hand or skip" to "Forge proposes the repair." (#1110,
  #1270)
- **User-declared model registry overlay in `forge.yaml`:** adding a model on
  an existing adapter no longer requires a TheForge release. The overlay
  composes with the built-in registry and is recorded in the audit. (#1184)
- **Canonical model identity scheme:** every model now has a stable canonical
  ID; historical profile records are aliased to the canonical ID so adaptive
  routing reads consolidated history. (#1106, #1291)
- **Profile maintenance command:** operators can reset, exclude, or audit
  contaminated model profile history without editing files by hand. (#1292)
- **Stack presets for root file conventions:** Python, Node, Go, Rust, and Java
  repos no longer have to hand-enumerate language-standard root files —
  `stack:` in `forge.yaml` selects a preset. (#1265)
- **`forge status --watch` live attach:** `--watch` attaches to a live sprint
  and degrades gracefully when state is partial; operators no longer need a
  perfect state file to see what's happening. (#1144)
- **Workspace hygiene gate:** repo tree mutations outside DEV are rejected so
  stray scratch files cannot strand subsequent runs. Project-configurable
  allowed-paths convention exposes this to non-default project layouts. (#1179,
  #1180)
- **Issue/story authoring guide:** a use-case-organized authoring guide,
  verified against code, lives in repo docs alongside the existing reference
  material. (#948)
- **Shape gate flags implementation-plan content:** issue bodies that contain
  Design sections, file paths, function names, or line numbers are flagged
  `needs-grooming` so HOW-leakage doesn't enter sprints as if it were WHAT.
  (#1273)
- **Plan-review per-reviewer audit detail:** plan-review audit summaries record
  each reviewer's pass/fail individually so debugging no longer requires
  grepping raw logs. (#952)
- **Migrated project lessons to repo-versioned `CONVENTIONS.md`:** project-level
  lessons that previously lived only in user memory are now repo-versioned and
  visible to every dev agent and reviewer. Shared CONVENTIONS.md replaces
  duplicated guidance across CLAUDE.md / AGENTS.md. (#1116, #1156)

### Changed

- **`budget_per_story_usd` removed; replaced by
  `assignment.max_cost_per_story_usd` (BREAKING):** the per-story routing cost
  cap is now a separate concept from sprint-wide budget, lives under the
  `assignment:` block, and defaults to `None` (no cap) instead of `$15.0`.
  Operators using the old key must rename it. (#1311, #1317)
- **Hardcoded `plan_agent_review.pool` removed from dogfood `forge.yaml`:**
  adaptive sizes the reviewer pool by complexity. Operators with `pool:` set
  in their own config should remove it. (#1268)
- **Routing policy SSOT:** score-to-tier threshold logic that previously lived
  in three places is consolidated into one source of truth so adaptive
  decisions are reproducible. (#1107)
- **Project memory propagated to spawned agents:** dev agents and reviewers
  spawned in worktree subprocesses inherit project memory instead of starting
  empty. (#1155)

### Fixed

- **Audit redaction destroyed context:** redaction replaced entire string
  values with `[REDACTED]` instead of substituting only the secret substring.
  Multi-line text containing a secret — story bodies, plans, command output —
  is now preserved with `[REDACTED]` in place of the secret only. Multiple
  distinct secrets in a single value are now all redacted. (#799)
- **Audit redaction bypassed tuples:** secrets stored in tuple values reached
  the on-disk audit unredacted because the recursive redactor had no tuple
  branch. Now covered. (#800)
- **Malformed YAML frontmatter silently dropped dependency declarations:**
  `depends_on` lines inside a story's broken frontmatter were lost (YAML parse
  failed, prose scan never saw the block). Now produces a visible operator
  warning at sprint start and an audit entry. (#880)
- **Sprint summary disagreed with audit on MERGE_FAILED state:** the queued-PR
  dep-poll-failure branch updated audit but not sprint-summary, so `forge
  status` showed DONE for deps that had actually failed to merge. State is now
  consistent across both surfaces. (#1277)
- **Disabled advisory issue filing reported prior issues as "newly filed" on
  every run:** the disabled-filing path overloaded one return channel for
  "preserved in artifact" and "newly filed this run." Separated. (#1274)
- **`forge status` MODEL column showed "panel(N)" for completed stories:**
  completed-story rows now show the dev model that did the work. (#1113)
- **`forge status` conflated ALREADY_DONE with missing-work failure:** the two
  states are now distinct in the operator-facing display. (#1200)
- **`forge status` showed only one sprint with multiple running:** all active
  sprints are visible. (#1203)
- **PRESERVED state applied to empty worktrees:** sprint no longer refuses to
  re-run a story whose "preserved" worktree contains no work. (#1201)
- **Worktree cleanup left empty `.forge/` subdirectories behind:** stale shells
  no longer accumulate in `.forge/worktrees/`. (#1190)
- **Sprint `--resume` manifest included already-merged stories then SKIPPED
  them with a misleading "workspace creation failed" message:** resume now
  filters and reports correctly. (#1129)
- **Cross-sprint conflict detection trusted stale lock files:** PID
  verification is required before treating a lock as live; single-story
  conflict no longer aborts the entire sprint; `--force` is honored. (#1264)
- **Story marked `final_phase: DONE` despite `landing_status: failed`:**
  terminal-state contradictions in audit and status display are eliminated.
  (#1262)
- **Advisory convention violations were recorded only in per-run audit and
  never aggregated:** rolling artifact aggregates and surfaces them so
  convention drift is operator-visible instead of accumulating in a black
  hole. (#1260)
- **Merge automation crashed on null-byte PR bodies:** LLM-produced PR bodies
  containing NUL no longer strand dev work. The offending input is captured
  with a real traceback. (#1256)
- **Reopen context never entered the sprint contract:** dev agents now reason
  from reopen comments, not just the original issue body. (#1271)
- **Preflight SIGKILL fallback was too permissive:** "conservative PROCEED" no
  longer lets reopened or contract-changed stories sprint without preflight
  verification. (#1272)
- **Dev phase had no retry on transient provider errors:** one API hiccup no
  longer escalates the story; transient failures are retried before
  escalation. (#1196)
- **Gate timeout with dev commits was misclassified as terminal:** treated as a
  retryable validation failure now; dev gets the timeout evidence and a chance
  to fix. (#1216)
- **Stuck-pattern detector false-terminated legitimate exploration:** the
  no-file-modifications arm is demoted to telemetry; termination requires
  high-confidence signal. (#1215)
- **CLI runner missed Codex "you've hit your usage limit" signature:** the
  fallback patterns and resumed-retry path now cover this case. (#1225)
- **Reviewers passed PRs without verifying acceptance criteria:** review
  contract now requires checking that ACs are actually fulfilled, not just
  that the diff is coherent — catching silent-contract-swap merges. (#1224)
- **Gate scrub left agent CLI binaries on PATH:** auth check now agrees between
  dev and CI; scrub removes provider CLIs along with credentials. (#1119)
- **Sprint dependency-prose parser fired false positives on code blocks:**
  parser ignores code-fenced content and emits accurate guidance. (#1114)
- **Sprint shape-check read stale `shape-check-v1` comments:** live label state
  is the source of truth; an issue with the `epic` label removed is no longer
  skipped. (#1186)
- **Local shape check keyword-matched against story body:** legitimate words
  like "umbrella" or "epic" used as use-case labels no longer trigger false
  `epic_or_tracking` skips. (#1192)
- **`plan_agent_review: enabled` silently injected `cli: claude` default:**
  adaptive routing can pick the plan reviewer instead of being preempted by
  the silent default. (#1310)
- **Sprint budget carried sunk cost from prior sprints:** budget enforcement
  is now strictly per-sprint. (#1053)
- **Sprint intake remediation called a deferred placeholder agent caller:**
  the configured agent-backed grooming path is invoked instead of the
  placeholder. (#1270)
- **Adaptive routing read fragmented model profile history:** canonical model
  identity consolidates history so promotions operate on full evidence rather
  than partial. (#1290)
- **`gate_timeout: 45` was too tight for parallel sprint load:** dogfood
  default raised; auto-scaling tracked separately for v0.11.0. (#1293)
- **Stale generated post-run hooks:** `forge check-config` now warns and exits
  non-zero when the configured generated findings hook is missing static
  labels or label setup required by the current template. The warning names
  the stale hook and describes how to refresh it.
- **Sprint bundling never fired:** documented bundling was a no-op because
  eligibility was gated on a per-story preflight flag the prompt never asked
  the LLM to set. Eligibility is now decided at the sprint scheduler level
  from preflight signals. (#1348)
- **Review summary "(persistent)" tag rendered next to wrong severity count:**
  the tag now describes the matched severity it actually refers to. (#955)

### Removed

- **`budget_per_story_usd`:** see Changed for the rename to
  `assignment.max_cost_per_story_usd`. (#1311)

### Documentation

- **Post-release doc review for v0.9.0:** verification template forced each
  claim to be checked against code, not against other docs. Outcomes folded
  into v0.10.0 as appropriate. (#1185)

## [0.9.0] — 2026-05-01

The headline of v0.9.0 is **adaptive intelligence**: complexity is now scored on
a 1–10 scale and drives model assignment, iteration limits, budgets, and timeouts
deterministically instead of via static config. Run history accumulates as model
profiles, escalation history feeds back into routing, and process overhead scales
with the work — small bounded bugs skip planning, LARGE stories get strong-tier
models with proportional budgets. Sprint reliability has been hardened against
the failure modes that destroyed work in 0.8: silent escalations, stale baseline
gates, lock-file races, status-display lies, and resume edge cases.

This is the first release shaped by the **dogfood-from-releases** principle:
pin your install to a tagged version and develop the next release against it.
Floor-blocker fixes for substrate quality (sufficiency over-classification,
planner tier divergence, audit-vs-execution match) landed late in the cycle to
make v0.9.0 safe to build v0.10.0 on, not just safe to run.

### Added

- **Granular complexity scoring (1–10):** preflight emits a numeric complexity
  score that drives every downstream adaptive decision instead of the legacy
  LOW/MEDIUM/HIGH band. Score is logged at `phase_end PREFLIGHT` and persisted
  to audit. (#119, #1102)
- **Adaptive model assignment:** complexity-driven routing picks dev, planner,
  reviewer, and preflight tiers per story; rationale is recorded in audit so
  operators can see why a model was chosen. (#283)
- **Adaptive resource budgets and iteration limits:** dev/review iteration caps,
  per-phase timeouts, and budgets derive from complexity and run history rather
  than static config. Profile history accumulates per model. (#156, #169, #510)
- **Timeout-triggered model escalation:** dev agents that hit timeout escalate
  to a stronger tier on retry, with the escalation visible in routing. (#359)
- **Progress-aware timeouts:** stuck dev agents are detected by lack of file
  modifications and terminated cooperatively before the wall-clock cap. (#157,
  #1071, #1130)
- **Preflight fallback on transient failure:** preflight retries with a fallback
  model profile when the primary fails. (#707)
- **`forge todo` capture path:** frictionless command for filing architectural
  debt and follow-up issues with a `todo:draft` label. (#857)
- **`forge status` enhancements:** rich per-story detail throughout sprint
  lifecycle, MODEL column, STAGE/DETAIL split, and `--watch` live-update mode
  modeled on `top`/`htop`. (#719, #1035, #1036, #1093)
- **Collision-derived DAG edges:** preflight `likely_files` automatically
  serialize colliding stories without requiring manual `depends_on`. (#1073)
- **Gate environment scrub:** `make gate` runs in a scrubbed environment with no
  agent credentials, CLI auth state, or dotenv autoload available. Tests that
  forget and call a real provider CLI fail fast under the scrub sentinel. (#900,
  #911)
- **Forge-config story-branch allowlist:** the gate now rejects story branches
  that mutate `forge.yaml` outside an allowlist of story-mutable keys. (#1001)
- **Release-time doc review template:** post-release doc review issue is
  auto-created against the next milestone with a verification template that
  forces checking each claim against code, not against other docs. (#947)
- **Examples are first-class in feature issues:** feature/enhancement issue
  format requires a concrete example or target sketch; shape-check enforces
  it. (#1040)
- **Language-agnosticism convention with mechanical check:** stack-neutral
  layers (`task/`, `coordinator/`, `sprint/`, shared schemas) are scanned for
  language-specific assumptions. (#931)

### Changed

- **Stack-neutral story schema (breaking):** `TaskStory.pytest_target` and
  `{pytest_target}` gate placeholder are renamed to `test_target` and
  `{test_target}`. Story scaffolding and ideation output no longer emit the
  field; gate scoping belongs in `forge.yaml`. Update story files and any
  `forge.yaml` gate commands referencing the old name. (#930, #940)
- **TransportSpec unification:** transport representation is unified across
  dispatch — provider/model identity and CLI/API transport are a single source
  of truth. CLI-first with API fallback is now the ergonomic default for
  mixed-transport reviewer pools. (#891, #892)
- **Schema repair fails closed:** APPROVE+P1 and REQUEST_CHANGES+no-P1 review
  outputs are now hard errors instead of being silently rewritten — review
  schema is the integrity boundary. (#996)
- **Reviewer prompt forbids closure-note findings:** reviewers are explicitly
  prevented from filing closure notes as P1 findings. (#998)

### Fixed

#### Adaptive routing

- **Sufficiency over-classifies as needs_planning:** trivial bounded work no
  longer runs the full plan + plan_review pipeline unnecessarily; bounded bugs
  skip planning when planning would buy nothing. (#1163)
- **Planner agent selection drops from strong to mid:** when the strong-tier
  model is already assigned to dev, the planner no longer silently falls to
  mid-tier with the audit reporting "tier strong." Audit and execution match.
  (#1164)
- **Adaptive dev_budget under-funds LARGE stories:** strong-tier-routed LARGE
  stories no longer escalate from budget exhaustion partway through legitimate
  work. (#1148)
- **Coordinator-override upgrades complexity from context keywords:** preflight
  no longer over-sizes complexity based on story prose vocabulary instead of
  fix-shape. (#1150)
- **Plan reviewers computed but never applied:** adaptive plan review no longer
  silently skips when the computed reviewer pool isn't propagated into config.
  (#922)
- **Preflight under-sizes concurrent multi-phase coordinator work:** complex
  changes spanning multiple coordinator phases get appropriate complexity
  scores. (#1136)
- **Preflight false-positives ALREADY_DONE:** adaptive-routing stories no
  longer get cited as already done from partial implementation. (#994)
- **Persistent-finding classifier collapses distinct findings:** P1 persistence
  matching now requires more than file-path equality, so different bugs in the
  same file are no longer treated as the same recurrence. (#956, #999)
- **Findings on changed files misclassified as regressions:** new findings on
  changed files require description match before being labeled regressions.
  (#997)

#### Sprint reliability

- **Sprint baseline gate runs against stale local main:** baseline now pulls
  origin before evaluating, so origin-side critical fixes don't false-negative
  the baseline. (#1120)
- **Sprint resume treats zero-delta APPROVE history as merged work:** resume
  no longer false-skips stories that completed in a prior run. (#1145)
- **Live sprint status reuses prior terminal story state during resumed run:**
  resumed sprints no longer carry stale terminal state into the live view.
  (#1146)
- **Sprint summary preserves stale escalation records when stories succeed
  later:** a successful re-run no longer leaves the original escalation in the
  summary. (#1030)
- **Sprint summary drops stories completed in earlier resumes:** stories
  completed before the final manifest entry are no longer omitted from the
  summary. (#958)
- **Sprint summary excludes tracked batch preflight cost when stories skip:**
  preflight cost is now attributed correctly even when stories are skipped.
  (#995)
- **Sprint-summary overwrites ALREADY_DONE outcome with ESCALATE on later
  run:** a later run that escalates no longer clobbers a prior ALREADY_DONE
  outcome. (#1047)
- **Lock sweep mistakes recycled PIDs for live forge runs:** unrelated
  processes that recycle a forge PID (imagent, Google Drive) no longer hold
  forge locks. (#959)
- **`forge stop` silent SIGTERM timeout:** `forge stop` no longer surrenders
  silently and leaves zombie processes holding locks. (#960, #1117)
- **Workspace abort on `git pull` failure escalates instead of recovering:**
  recoverable workspace failures no longer escalate the story. (#1048)
- **Stale-worktree sweep destroys uncommitted work on escalated stories:**
  worktree cleanup respects in-progress and recently-escalated work. (#939)
- **Worktree lifecycle hygiene:** worktrees no longer leak debris into
  `.forge/worktrees/`. (#928)
- **Adaptive timeout coverage gap:** plan-phase and sprint-worker timeouts
  now derive from complexity instead of running with hard-coded defaults.
  (#1070)
- **Preflight cache survives across sprint runs even after code changes:**
  the cache validates the worktree HEAD and base-branch HEAD before reuse.
  (#1015, #1082)
- **Collision-derived DAG edges are now soft:** when an upstream story
  escalates without merging, the soft edge is dropped instead of stranding the
  downstream forever. (#1112)

#### Dev / review correctness

- **Dev agent that produces zero commits is treated as APPROVE/DONE:** empty
  diffs now escalate instead of being silently approved. (#1127)
- **Stuck-detection false-terminates dev agents during legitimate codebase
  exploration on LARGE stories:** stuck thresholds now scale with complexity
  and plan size. (#1130)
- **Plan-review has no retry for transient provider errors:** a single 500
  no longer shrinks the reviewer pool. (#951)
- **Hard convention violations not propagated to dev retry:** convention
  failures now reach the dev agent as actionable findings. (#1017)
- **Runner subprocess failures masked behind generic "no changes":** runner
  failures now surface with their actual cause. (#1016)
- **Claude CLI runner hangs after producing output:** SIGKILL no longer
  discards valid plan/preflight/dev output captured in stdin/stdout. (#1054)
- **Profile transport mutation produces malformed dispatch state:** profile
  resolution no longer mutates the transport in-place. (#890)
- **`update_finding_registry` crashes on mixed str/int `Finding.line`:** mixed
  types are normalized before registry write. (#908)

#### Status display

- **`forge status` shows stale review/detail data for re-run stories:** DETAIL
  no longer leaks from prior run; non-success completed rows ignore stale
  verdicts. (#1128)
- **`forge status`: ELAPSED column blank, SKIPPED rows show no reason:**
  ELAPSED populates from timestamps; SKIPPED rows now carry their reason.
  (#1080)
- **`forge status --watch` silently falls back to one-shot during sprint
  startup:** the watch mode now waits through the startup window instead of
  collapsing to a snapshot. (#1089)
- **Single source of truth for sprint story state:** status, baseline, and
  resume now read from a unified state representation. (#1065)
- **`forge status` integration tests cover sprint lifecycle and re-exec
  edges:** lifecycle and resume edges are covered by integration tests.
  (#1049)

#### Conventions and runtime

- **Forge gate must run scrubbed:** removes ambient credentials before
  invocation; no real-CLI calls in the default gate. (#900, #911)
- **Claude CLI runner uses OS-level write containment:** matches Gemini
  containment, prevents agent writes outside the worktree. (#749)
- **macOS bash-tool sandbox profile makes `pytest`/gate unusably slow:**
  sandbox profile tuned to remove the slowness. (#929)
- **Codex resume runner passes unsupported `-C/--cd` flag:** removed from
  `codex exec resume` invocation. (#1013)
- **Runner seam-test gap:** runner tests now exercise subprocess lifecycle
  via fake-CLI fixtures instead of mocking `subprocess.Popen`. (#1056)
- **Shape-check uses single `needs-triage` label:** removed invented
  `triage-*` vocabulary in favor of the canonical label. (#1075)
- **`post_run` hook files findings in wrong bug format:** hook now follows the
  bug-report convention instead of feature-format. (#882, #883)
- **`forge.yaml` gate command honors scrubbed-gate contract:** dogfood
  configuration points at `make gate` instead of bypassing the scrub. (#911)
- **`_has_base_commit_referencing_issue` over-matches base commits:**
  base-commit reference checks now require structural match, not substring.
  (#875)
- **Convention 6 evidence requirement:** new data flowing between coordinator
  phases must be visible in the audit trail. (#877)


## [0.8.0] — 2026-04-20

### Added

- **Simple `models:` config path:** `forge.yaml` can now declare a model list and
  `budget_usd`; TheForge derives preflight, plan, dev, review, and synthesis roles
  automatically instead of requiring hand-written `profiles:` blocks. (#807, #816, #820, #822)
- **Explicit AgentSpec transport model:** provider/model identity and CLI/API transport are
  now represented separately, so `check-config` reports API-backed models accurately and
  Google models route through the intended API transport. (#861, #865, #866)
- **Structured audit expansion:** per-run audit files now capture story text, plan content,
  run identity, and redacted contract data for post-run diagnosis. (#798, #802, #804)
- **Forge-owned handoff artifact:** the coordinator owns the handoff contract instead of
  trusting a dev-written file for validation decisions. (#827)
- **Story shape checks:** issue drafting and sprint entry now enforce story shape, with a
  `forge sprint --force` escape hatch that prints skipped reason codes. (#867, #871, #879)
- **Dependency metadata parsing:** issue dependencies are read from structured metadata, with
  prose treated as an authoring warning instead of the source of scheduling truth. (#819, #881)

### Changed

- **Dogfooding config moved to v0.8 schema:** this repository's `forge.yaml` now uses the
  simple model-list configuration path. (#833, #855)
- **Legacy config removed from the v0.8 path:** `profiles:`, `smart_config_models:`, and
  legacy `agents:` are rejected when mixed with `models:`; use `models:` plus `overrides:`
  for derived-role configs. (#854)
- **Preflight/model assignment guardrails:** role derivation now prevents bad
  tier/complexity combinations and keeps model assignment visible through `check-config`.
  (#815, #822)

### Fixed

- **Gate and timeout diagnosis:** multi-stage gate commands no longer produce false
  CONTRADICTION results, and timed-out gates run `gate_debug_command` before escalation.
  (#826, #838)
- **Sprint scheduling and resumption:** stale no-pull behavior, synthetic dependency cycles,
  dropped completed stories, mid-sprint re-exec aborts, closed dependency issues, and closed
  manifest dependencies were fixed. (#805, #814, #832, #841, #853, #878)
- **Run status accuracy:** stopped processes and orphaned runs no longer remain permanently
  RUNNING in `forge status`. (#844, #845, #846)
- **Squash-merge detection:** sprint triage no longer relies only on audit-trail APPROVE to
  detect externally merged branches. (#876)
- **Subprocess environment isolation:** agent subprocesses avoid poisoning global Python when
  a bare `pip` or `python` appears in commands. (#864)
- **Self-hosting gate/runtime regressions:** localhost network and sandbox-related gate
  failures from this release cycle were corrected. (#849, #850)

### Removed

- **File-based handoff contract removed (breaking):** `handoff_file` and `gate_decision_key` are
  no longer accepted under `validation:` in `forge.yaml`. The gate now signals pass/fail via exit
  code only. Remove these keys from your `forge.yaml` — the config loader will error on startup if
  they are present. The `make gate` target no longer writes `.forge/handoff.yaml`. (#825)

## [0.7.0] — 2026-04-13

### Added

- **Native CLI sandbox write containment:** Claude and Codex CLI runners use provider-native sandbox flags (`--sandbox`, `--no-write-outside-sandbox`) to confine agent writes to the worktree — replacing the file-system sibling-worktree detector (#654)
- **Plan structure validation:** plan output is checked against a mechanical structure schema before DEV begins — empty or schema-invalid plans are rejected rather than passed on (#335)
- **Self-review prevention for plan reviewers:** the plan reviewer pool now excludes the same model used as planner, ensuring independent review (#333)
- **Plan review JSON schema enforcement extended to plan path:** API-mode plan reviewers now receive full `response_schema` enforcement, matching what code reviewers already had (#332)
- **Model preference lists for best-available provider fallback:** forge.yaml profiles can declare an ordered preference list of models; the coordinator picks the best available at run time (#326)
- **Auto-downgrade reviewer findings contradicted by gate:** findings that the gate run does not reproduce are automatically downgraded so false positives no longer block APPROVE (#320)
- **Global provider SDK isolation guard in tests:** a pytest fixture injects a socket guard that fails the test if real provider API calls are made during the unit test suite (#684)
- **Preflight prompt hardening:** bounded forensic classifier prompt reduces hallucinated BLOCKED verdicts on ambiguous specs (#700)
- **Unsubstantiated fix claims flagged to reviewers:** when a dev handoff claims fixes without matching code evidence, the flag is surfaced in the review prompt (#257)
- **`forge sprint-status` command:** shows all stories and their live phase for a running sprint (#492)
- **`forge status` unified surface:** `forge status` is now the single surface for active runs and pending decisions — previously split across multiple commands (#711)

### Fixed

- **Gate parser false FAIL on successful output:** gate output containing "All checks passed!" or similar success signals is now correctly parsed as PASS even on non-zero exit codes (#740)
- **Preflight malformed/empty output no longer becomes BLOCKED:** a parse failure in preflight output defaults to PROCEED rather than halting the story (#709)
- **Preflight ALREADY_DONE is deterministic:** verification is now assessed against a clean baseline rather than a potentially stale cached verdict (#705)
- **Preflight failure no longer blocks the story:** a coordinator crash during preflight now escalates the story rather than silently marking it BLOCKED (#699)
- **NoneType crash in cached preflight resume path:** fixed a crash in `engine.py _resume_body` when resuming a run that had a cached preflight but no associated state (#675)
- **ALREADY_DONE stories no longer run DEV in sprint/batch mode:** coordinator correctly short-circuits to DONE when preflight returns ALREADY_DONE (#674)
- **Review diff uses three-dot notation:** review phase now uses `git diff A...B` (three-dot, merge-base diff) rather than two-dot, eliminating phantom regressions when parallel stories diverge (#770)
- **Parallel worktrees no longer race on git fetch:** `git fetch` calls across parallel sprint workers are serialized to prevent `incorrect old value provided` failures (#754)
- **`_extract_failed_tests` no longer records xdist worker IDs:** pytest-xdist worker prefix lines are filtered out so only real test names are injected into retry prompts (#681)
- **Dev prompt prevents tests depending on optional provider SDKs:** system prompt explicitly prohibits importing optional SDK packages in test files, preventing CI failures in minimal environments (#683, #682)
- **Preflight early-exit verdict writes audit:** when preflight returns ALREADY_DONE or BLOCKED, the audit trail is now written before the coordinator exits (#678)
- **Dev runs pytest with xdist parallelism:** the gate command passed to dev agents now includes `-n auto --dist worksteal`, matching what the coordinator uses (#574)
- **Sprint log lines now include timestamps:** log lines emitted during sprint execution carry ISO timestamps for tracing latency (#573)
- **Review no longer hard-fails on misreported commit list:** when a dev agent's handoff lists incorrect commits, the review phase uses the actual worktree commits rather than failing (#572)
- **Gemini profile fixture missing `sandbox_mode=none` fixed in CI:** Linux CI failures caused by a missing `sandbox_mode` key in the Gemini test fixture are resolved (#702)
- **PID file removed on unhandled exception in daemon:** the daemon now cleans up its PID file even when `run_task` raises an unexpected exception (#137)
- **Daemon `state_update_fn` called at all phases:** state updates are now emitted at WORKSPACE, PREFLIGHT, PLAN, and all subsequent phases — not only at DEV and later (#104)
- **`--until init` stop check fires at correct phase:** `forge run --until init` now stops before WORKSPACE rather than after it (#100)
- **`git log` failure no longer silently continues workspace-branch collision check:** when `git log` fails, the collision check aborts rather than treating it as no collision (#98)
- **Budget enforcement includes planner and plan reviewers:** `_enforce_budget` now accounts for plan-phase costs when evaluating the remaining budget (#95)
- **Conversation JSON dump on max-iterations uses correct path:** the debug dump created when an agent hits max iterations is written to the correct per-run path (#39)
- **Review convergence corroboration uses fuzzy fingerprint matching:** corroboration grouping now tolerates minor wording differences across reviewers, reducing false-negative corroboration (#34, #31, #30)

### Changed

- **Sibling-worktree write detector retired:** the file-system level sibling detector is replaced by provider-native sandbox containment (#654)
- **Preflight BLOCKED verdict is now only emitted when the spec itself is invalid** — coordinator crashes and malformed output no longer masquerade as BLOCKED (#699, #709)

## [0.6.0] — 2026-04-12

### Added

- **`forge index` command:** generates a structural index of the codebase (modules, classes, functions) to give agents accurate navigational context before they begin work (#418, #419)
- **Phase-aware ContextAssembler:** assembles relevant codebase context per pipeline phase (preflight, plan, dev, review) and injects it into prompts; index is regenerated as part of `make gate` (#420, #421, #422)
- **Iteration-level telemetry:** per-iteration cost, duration, tool calls, and token counts are instrumented into the audit trail for every agent invocation (#509)
- **Sibling-worktree write detector:** parallel CLI workers that write to the same source files are detected and rejected before the collision corrupts both branches (#625, #638)
- **Deterministic bug bundling:** small bugs in the same area are grouped into a single dev execution when preflight marks them as bundle candidates, reducing per-story overhead (#551)
- **Conventions check runs with gate:** convention violations (line length, circular imports, module size) are checked in parallel with the gate rather than as a post-gate afterthought — failures surface earlier (#611)
- **Identical-failure circuit breaker:** gate retries are cut short when the same failure repeats verbatim, preventing wasted dev iterations on a deterministically broken gate (#598)
- **Directory-level `CLAUDE.md` files:** subsystem-local context files added for `coordinator/`, `runners/`, `sprint/`, `task/`, `config/`, and `cli/` to guide agents working in those areas (#417)

### Fixed

- **Landing status is now authoritative:** `CoordinatorResult.success` reflects merge/push outcome separately from review approval — "approved but failed to land" is a distinct, visible outcome in the audit trail (#600)
- **Merge-pr is resumable and fail-closed:** `_merge_pr` is broken into discrete steps with state checkpointing; pending auto-merge polls are bounded and fail closed on timeout rather than hanging the sprint indefinitely (#607, #632, #633)
- **Landing serialized in scheduler thread:** parallel workers no longer race to merge — the scheduler thread holds the integration lock, preventing interleaved squash-merges from corrupting the base branch (#626)
- **Dependent stories no longer skip on `pending_integration`:** stories waiting on a dependency that was queued for auto-merge were incorrectly classified as permanently blocked; they now wait correctly and unblock when the dependency lands (#642)
- **Workspace fails closed on diverged base branch:** when `git pull --ff-only` fails because local and origin have diverged, the sprint aborts with a clear error instead of creating a worktree from stale local state (#661)
- **Preflight evaluates clean baseline, not stale worktree:** `ALREADY_DONE` verdicts are assessed against the current base branch, not a previously-created worktree that may be behind origin (#588)
- **Invalid preflight and handoff are hard stops:** a non-`PROCEED` preflight verdict and an invalid dev handoff (after retries) both halt the story rather than normalizing to proceed-anyway behavior (#597, #596)
- **Handoff self-reporting removed:** dev agents could previously declare `gate: PASS` in their own handoff, bypassing actual gate execution; self-reported gate results are now rejected (#582)
- **Handoff repair is content-aware:** the handoff repair path now validates repaired content against the story's acceptance criteria, not just schema structure (#544)
- **Stale handoff capture closed:** the coordinator no longer reads a handoff file left over from a prior iteration when the current dev pass produced none (#543)
- **Cross-worktree filesystem isolation:** parallel workers are prevented from reading or writing files that belong to a sibling worktree (#545)
- **Retry feedback surfaces failing tests:** when gate retries fail, the extracted failing test names and likely culprit paths are injected into the next dev prompt (#583)
- **Dev prompt prohibits editing unrelated tests:** the dev system prompt now explicitly forbids fixing regressions by modifying tests outside the story's scope (#584)
- **Preflight classifies contract changes as `needs_planning`:** cross-cutting changes that alter shared interfaces are now classified as requiring the plan phase, rather than being sent directly to dev (#586)
- **Preflight `likely_files` always populated:** a missing `likely_files` field in preflight output no longer leaves the collision scheduler blind — the field defaults to empty list rather than being absent (#548)
- **Hard convention violations no longer burn a dev iteration:** a convention violation detected before dev begins now aborts early rather than letting dev proceed and failing at gate (#467)
- **Integration force-push after PR merge fixed:** the sprint no longer attempts a force-push to a remote branch that GitHub has already deleted after auto-merge (#532)
- **Audit reports finding injection:** P1 findings carried forward into subsequent dev prompts are now recorded in the audit trail (#546)
- **gpt-5.4 pricing added:** missing cost entry for gpt-5.4 caused dev costs to show as $0 in the audit and sprint summary (#547)
- **Dev agent scratch files caught by write detector:** throwaway exploration files created outside standard project directories during dev are flagged and blocked from landing on main (#650)
- **Re-exec after `git pull`:** when the sprint runner pulls new forge source code at startup, the process re-execs so the updated code is actually running before work begins (#646)
- **CLI launcher sandbox wrapping removed:** CLI runners (Claude, Codex) were being sandbox-wrapped despite `forge.yaml` explicitly disabling sandbox — removed the erroneous wrapping (#599, #624)
- **Coordinator HTTP calls are bounded:** all outbound `urllib` and `gh` subprocess calls now carry explicit timeouts so a hung connection cannot block the sprint indefinitely (#292)
- **Reviewer demotion counter resets between review cycles:** a reviewer demoted in cycle 1 was permanently demoted for the run; demotion state now resets at the start of each new review cycle (#610)
- **Audit iteration counts match actual invocations:** dev and review iteration counts in the audit trail now reflect actual agent calls rather than retry-loop bookkeeping values (#601)
- **Sandbox availability recorded in state and audit:** whether the sandbox is available at run start is captured in coordinator state and surfaced in the audit trail (#604)
- **Duplicate findings deduplicated in review merge:** identical findings from multiple reviewers in the same pool are collapsed before being surfaced or carried forward (#605)
- **Multi-line acceptance-criteria bullets parsed correctly:** `_extract_story_acceptance_criteria` was truncating wrapped markdown bullets at the first newline, causing valid dev handoffs to fail story-consistency checks (#666)

### Changed

- **Auto-merge and parallel execution disabled for self-hosting:** auto-merge and parallel sprint workers are temporarily disabled for self-hosting runs until `sys.path` isolation and landing monotonicity are fully validated (#594, #595)
- **Auto-model escalation frozen behind feature flag:** automatic model tier escalation on repeated failures is disabled by default; enable via `forge.yaml` when the behavior has been validated (#609)
- **Net-new P1 bypass frozen behind feature flag:** the rule allowing a net-new P1 finding to bypass escalation is disabled by default (#608)
- **Bug report convention documented:** CLAUDE.md now specifies that bug stories contain only observed behavior + expected behavior — no acceptance criteria, file paths, or implementation hints (#549)

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
