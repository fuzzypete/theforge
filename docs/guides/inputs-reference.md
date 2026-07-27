# Inputs Reference

Every file format TheForge accepts as input.

---

## Story (GitHub issue or `stories/*.md` file)

The primary input. Describes WHAT to build and WHY. The dev agent implements
exactly what the acceptance criteria say. Stories may live as **GitHub
issues** (the mode TheForge itself uses) or as **local files** under
`stories/*.md` (or any path); `stories/` is simply the default directory
created by `forge init`. Content rules are identical for both backends.

For how to write a good story — required sections, per-use-case templates,
and worked examples for features, bugs, refactors, rollups, and docs/chore
work — see the **[Authoring Guide](authoring.md)**. This page covers only
file format and frontmatter.

### Local file format

A local story file is a markdown file with optional YAML frontmatter:

```markdown
---
name: "Short human-readable title"
slug: my-feature-slug
---

# Story Title

(body — see the Authoring Guide for the per-use-case body templates)
```

GitHub issues do not need frontmatter; the slug is derived from the issue
title (or overridden in the sprint manifest), and the issue title supplies
`name`.

### Frontmatter fields (local-file mode)

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Human-readable title. Shows in logs and audit. |
| `slug` | Yes | — | Branch name (`forge/{slug}`), worktree path. Lowercase-with-dashes. |
| `test_target` | No | `.` | Stack-neutral test target substituted for `{test_target}` in the gate command. |
| `gate` | No | project default | Override gate: `"none"` (skip), `"lint"`, or custom command. |
| `depends_on` | No | `[]` | Slugs that must be merged before this story runs (sprint mode). |

---

## Sprint manifest (`sprints/*.yaml`)

Bundles multiple stories into a sequential run with shared budget. You can also
run sprints without a manifest using `forge sprint --milestone` or `--label`
(see [CLI Reference](cli-reference.md#forge-sprint)).

### Template

```yaml
name: "Sprint Name — brief description"
budget_usd: 50
auto_merge: true    # optional: merge each APPROVED story automatically
stories:
  - stories/story-one.md           # local story file
  - stories/story-two.md
  - {issue: 123}                   # pull from GitHub issue #123
  - {issue: 124, slug: my-feature, depends_on: [story-one]}
```

> **Note:** `specs:` is a deprecated alias for `stories:` and still works.

### Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Human-readable sprint name |
| `budget_usd` | Yes | — | Total budget ceiling across all stories |
| `auto_merge` | No | `false` | Auto-merge approved stories to main |
| `stories` | Yes | — | Ordered list of stories — local file paths or `{issue: N}` dicts |

### Cost governance vs. per-story estimates (converged model)

TheForge has one converged budget view with two distinct roles for dollar
values. Keeping them straight explains why a sprint stops but an individual
story never gets blocked for going "over budget."

- **Sprint-level governance (enforced).** The sprint `budget_usd` is the *only*
  post-hoc dollar enforcement. When cumulative sprint spend reaches it, no new
  stories launch. This is the surviving cost-governance surface.

- **Per-story dollar values (estimates, not enforced).** The per-story dollar
  value is a *cost estimate* derived from historical cost data for the model and
  complexity band (`adaptive_dev_cost_estimate_usd` in state;
  `chosen_dev_cost_estimate_usd` in the audit trail). It informs routing,
  timeout scaling, and telemetry — it is **not** a cap. Exceeding it is never,
  by itself, an operator-actionable overrun; it just means the estimate was low.

  - If a dev attempt exceeds its estimate **and committed usable work**, the run
    proceeds to validate/review with no operator action. The estimate was low.
  - If a dev attempt exceeds its estimate **and produced no commits**, the run
    escalates as an *unproductive attempt* — the message names that no usable
    output was produced (`Dev attempt produced no usable output (...)`), not a
    dollar overrun.

- **Pre-runaway controls (unchanged).** Iteration and timeout adaptation still
  cap runaway loops *before* they burn cost. The adaptive routing cost target
  (`assignment.max_cost_per_story_usd`) is likewise a *pre-run* control: it
  shapes model selection (downgrading tiers so the estimated per-story routing
  cost stays under the operator's configured value) before any dev/review work
  runs. Operator and audit surfaces call it a "routing cost target," not a
  "cap," precisely so it is not confused with post-hoc dollar governance. All of
  these are separate from post-hoc dollar accounting and unaffected by the
  converged model.

The per-role `budget_usd` on individual profiles (below) seeds the per-story
estimate baseline and, for reviewers, still acts as an explicit operator-set
per-reviewer ceiling. It is not the story-level governance surface — that is the
sprint `budget_usd` above.

### Recency weighting (`assignment.recency`)

The adaptive router ranks dev models on their historical success rate. By default
that history is **recency-weighted** so a model's *recent* behavior drives routing
rather than a lifetime cumulative average — old failures decay out of relevance on
their own timeline instead of permanently penalizing a model whose current behavior
is fine (ADR-0006 clause 2.4). This composes cleanly with the sample floor: below
`min_runs` admissible runs a model is still cold-start (no ranked rate), and
affirmatively tainted runs are excluded from the weighted aggregate entirely.

The defaults need no tuning for ordinary use. To adjust:

```yaml
assignment:
  recency:
    mode: exponential     # exponential | window | off
    half_life_runs: 50    # (exponential) a run's weight halves every N runs
    window: 200           # max recent outcomes consulted (also caps `window` mode)
```

- `exponential` (default): a run's weight decays by half every `half_life_runs`
  runs of newer history. Deterministic and recomputable from stored admissible
  outcomes after any parameter change (run position, not wall-clock, is the age
  axis — profiles store an ordered outcome ring, not timestamps).
- `window`: an unweighted mean over the last `window` runs (a fixed-window cutoff).
- `off`: no recency weighting — routing falls back to the lifetime cumulative rate.

Invalid values (unknown `mode`, non-positive `half_life_runs`/`window`) are rejected
at config load rather than silently defaulted. The raw cumulative rate, the weighted
rate actually used, the admissible sample count, the sample-floor result, and any
taint-excluded count are all recorded per candidate in the `routing_decision` audit
block so raw-vs-weighted divergence is visible.

### Story bundling (relational, scheduler-decided)

When a sprint contains two or more eligible stories, the sprint scheduler may
group them into a *bundle*. Bundling eligibility is **relational** — it is
recomputed at sprint-schedule time after every story's preflight has run, and
is decided by the coordinator from objective signals. There is no
per-story flag the preflight agent emits to opt in.

A pair of stories is bundle-eligible when **all** hold:

- Both have `work_type` in `{bug, mechanical}` and `complexity == small` (the
  per-story prerequisite — bundling is restricted to bounded, low-blast-radius work).
- The pair has positive evidence of code overlap: matching `Area:` label in the
  story body, **or** an intersection of known `likely_files` reported by preflight.
- Neither depends on the other (no manifest `depends_on` cycle into the bundle).
- The combined complexity weight stays under the bundle ceiling.

**Asymmetric overlap defaults.** Bundling and collision-DAG serialization use
opposite defaults when footprint information is missing:

- *Bundling* is fail-closed against unknown footprint: if either story's
  `likely_files` is `None` and they share no `Area:` label, the pair is **not**
  bundled. Gluing unrelated work into one PR is a worse failure than running
  serially.
- *Collision-DAG serialization* is fail-closed in the opposite direction:
  unknown footprint forces serialization, because letting an undetected conflict
  run in parallel is worse than over-serializing safe parallel work.

The `bundle_candidate` field in per-story audit dumps is **scheduler-written
audit output** — it reflects "the scheduler placed this story in a bundle",
not anything the preflight agent asserted.

### Story entry formats

| Format | Description |
|--------|-------------|
| `stories/my-feature.md` | Local story file (path relative to project root) |
| `{issue: 123}` | Pull story body from GitHub issue #123 |
| `{issue: 123, slug: my-slug}` | Override the slug derived from the issue title |
| `{issue: 123, depends_on: [other-slug]}` | Declare a dependency on another story in the sprint |
| `{issue: 123, test_target: tests/test_foo.py}` | Override the test target substituted into the gate command for this story |

### Declaring dependencies in GitHub issue bodies

Forge extracts dependencies from prose in GitHub issue bodies. Use **`Depends on: #N`**
as the preferred spelling when authoring or generating issues. `Blocked by #N` is
supported as a compatibility alias.

All of the following forms are recognized and normalized to `issue-N` slugs:

- `Depends on #265` — simple hash form
- `Depends on: #265` — colon form (preferred)
- `depends on #265` — lowercase
- `depends_on: #265` — underscore colon
- `depends_on: issue-265` — slug form
- `depends_on: [issue-265, issue-807]` — YAML list form (multiple dependencies)
- Full GitHub issue URLs in any of the above positions
- `Blocked by #265` — compatibility alias (still supported)

Multiple dependencies in a single body are all extracted. Native GitHub
`blocked_by` timeline relationships take precedence over body-text parsing
when available.

### Behavior

- Stories run in order. Each goes through the full pipeline.
- A failed/escalated story doesn't block subsequent ones.
- Budget is shared — remaining budget carries to the next story.
- `--resume` flag auto-triages each story's worktree state.

---

## Project configuration (`forge.yaml`)

Controls everything: which models to use, budgets, timeouts, retry policies.

### Minimal config

```yaml
project: my-project

models:
  - claude/sonnet
  - claude/opus

budget_usd: 30.0

workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} main"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  gate_command: "python -m pytest tests/ -q"

retry:
  max_dev_iterations: 3
  max_review_cycles: 2
```

In v0.8, `models:` is the primary config path. TheForge derives preflight,
plan, dev, review, and synthesis roles from the model list and the story's
complexity. Use `forge check-config` to inspect the derived role table.

### Full config reference

```yaml
project: my-project                # project name for logging/audit

# ── Model list and derived roles ──────────────────────────
models:
  - claude/sonnet
  - claude/opus
  - openai/gpt-5.4

budget_usd: 50.0                   # budget used to derive per-role ceilings

# ── Dev-phase policy knobs ────────────────────────────────
dev:
  p2_policy: in_scope              # "in_scope" (default) | "all" | "p1_only"

# Optional targeted changes to derived roles. Do not mix top-level profiles:,
# smart_config_models:, or agents: with models:.
overrides:
  dev:
    timeout_seconds: 1200
  review_pool:
    - name: review-strong
      timeout_seconds: 600

# ── Workspace ──────────────────────────────────────────────
workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} main"
  setup_command: "pip install -e ."    # optional: run once after worktree creation
                                      # {forge_python} expands to python_interpreter
  python_interpreter: "python3.12"    # interpreter this project develops against;
                                      # required when setup_command uses {forge_python}.
                                      # Worktree virtualenvs built from a different
                                      # interpreter are deleted and reprovisioned, so
                                      # the gate never inherits whichever Python
                                      # TheForge itself happens to be installed under.
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"
  base_branch: "main"                 # default: "main"
  on_approve: "none"                  # "none" | "merge" | "pr" | "merge-pr"
  merge_strategy: "squash"            # "squash" | "merge" | "rebase" (used by merge-pr)
  pr_labels: []                       # labels applied when on_approve is "pr" or "merge-pr"
  pr_draft: false                     # create PR as draft when on_approve is "pr"
  merge_wait_timeout_seconds: 3600    # max wait for queued merge-pr landing
                                      # (only pending checks consume it; a PR whose
                                      #  required checks have failed is abandoned at once)

# ── Validation gate ────────────────────────────────────────
validation:
  gate_command: "make gate"            # must exit 0 on success
  gate_timeout: 600                    # seconds; default varies
  gate_debug_command: ~                # optional: runs after gate_timeout for diagnostics
  gate_debug_timeout: ~                # seconds; default: same resolved value as gate_timeout
  gate_diagnostic_enabled: true        # run the serialized hang-diagnostic pass on gate timeout
  gate_diagnostic_command: ~           # override the built-in diagnostic command; ~ uses the default
  gate_diagnostic_per_test_timeout: 10 # seconds; per-test hard timeout applied in the pass
  gate_diagnostic_budget: 60           # seconds; hard wall-clock cap so the pass can't itself hang
  test_command: ~                      # optional command agents may run during dev
  pre_validate_command: ~              # optional: run before dirty check
  default_test_target: "."             # {test_target} substitution when no story test_target applies (e.g. baseline gate)
  failed_test_pattern: ~               # optional regex to extract failing-test names from non-pytest gate output;
                                       # takes named group "test", else group 1, else the whole match. When unset,
                                       # core uses its built-in pytest grammar and, if the output isn't pytest-shaped,
                                       # records visibly (log + audit) that extraction did not apply.
                                       # Example: 'Test Case .* (?P<test>\w+)\(\) failed'

# ── Retry policy ───────────────────────────────────────────
retry:
  max_dev_iterations: 3      # dev attempts within one review cycle
  max_review_cycles: 2       # full dev→review loops before ESCALATE
  max_review_parse_retries: 2  # reviewer output parse/schema error retries
  max_plan_regen_attempts: 3 # plan review reject → regeneration cycles

# ── Classic manual profiles (advanced alternative) ─────────
# Omit models: when using classic profiles. This path remains supported for
# fully manual role control, but it is not the v0.8 default.
profiles:
  # Dev agent (implements the story)
  dev:
    cli: claude                # CLI mode: "claude", "codex", or "gemini"
    # provider: openai         # API mode: "openai", "anthropic", "google", "deepseek"
    model: sonnet
    budget_usd: 5.00
    timeout_seconds: 600
    timeout_medium_seconds: 900     # optional: for medium-complexity stories
    timeout_large_seconds: 1800     # optional: for large-complexity stories
    max_iterations: 50              # optional: API-mode agent loop ceiling
    allowed_tools:
      - Read
      - Edit
      - Write
      - Bash
      - Glob
      - Grep

  # Preflight agent (classifies spec before dev)
  preflight:
    cli: claude
    model: sonnet
    budget_usd: 1.00
    timeout_seconds: 300
    allowed_tools: [Read, Bash, Glob, Grep]

  # Review pool (one or more reviewers)
  review_pool:
    - name: claude-reviewer      # unique name for logging
      cli: claude
      model: opus
      review_role: correctness   # optional: "correctness", "patterns", "edge-cases"
      budget_usd: 5.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]

    - name: codex-reviewer       # API-mode reviewer example
      provider: openai
      model: o4-mini
      review_role: patterns
      budget_usd: 2.00
      timeout_seconds: 300
      max_iterations: 50
      allowed_tools: [Read, Bash, Glob, Grep]

    - name: gemini-reviewer      # Gemini with extended thinking enabled
      provider: google
      model: gemini-2.5-flash
      review_role: edge-cases
      thinking_budget: 2048      # optional: enables Gemini thinking mode (token budget)
      budget_usd: 1.00
      timeout_seconds: 300

    - name: local-reviewer       # Local model via Ollama/vLLM/LM Studio
      provider: openai
      model: codellama
      base_url: http://localhost:11434/v1
      budget_usd: 0.00
      timeout_seconds: 60
      allowed_tools: [Read, Glob, Grep]

# ── Plan phase (optional) ─────────────────────────────────
plan:
  enabled: true
  cli: claude
  model: sonnet
  budget_usd: 1.00
  timeout: 600

# ── Plan review (optional) ────────────────────────────────
plan_agent_review:
  enabled: true
  pool:
    - name: claude-plan-reviewer
      cli: claude
      model: opus
      budget_usd: 2.00
      timeout_seconds: 600
      allowed_tools: [Read, Glob, Grep]

# ── Notifications (optional) ──────────────────────────────
notifications:
  backend: ntfy               # "none", "ntfy", "slack", "osascript"
  ntfy:
    priority: high
    # url resolved from NTFY_URL in .forge/.env

# ── Inline intake remediation (optional, opt-in fallback) ──
# Defaults to disabled. See "Inline intake remediation" below.
intake:
  grooming: false             # opt-in inline shape/grooming repair at sprint entry
  auto_fix: false             # allow a single agent rewrite pass on failure
  auto_fix_mode: comment      # "comment" (post + drop) | "edit" (rewrite body, rerun once)

# ── Sandbox capability profile (optional) ─────────────────
# Omit entirely for default write containment. See "Sandbox capability
# profiles" below.
sandbox:
  capability_profile: xcode   # forge-owned preset name; omit for the default

# ── Secrets (optional) ────────────────────────────────────
# API keys are read from .forge/.env (run `forge secrets-init` to create)
```

### Profile modes: CLI vs API

| Setting | CLI mode | API mode |
|---------|----------|----------|
| Config key | `cli: claude` | `provider: openai` |
| How it runs | Subprocess (`claude`, `codex`, `gemini`) | HTTP API call |
| Tool execution | Agent's own runtime | TheForge's tool runtime |
| When to use | Dev agent (needs full editor access) | Reviewers (read-only analysis) |
| `allowed_tools` | Forwarded to CLI as flags | TheForge executes tools locally |
| Cost tracking | Parsed from CLI output | Calculated from token usage |

### Inline intake remediation (`intake.grooming`)

`intake.grooming` is an **opt-in fallback**, not the primary readiness workflow.
It is disabled by default (`grooming: false` in the schema), and the recommended
path is to make an issue shape-gate-clean with **`forge groom <issue>` before
sprint selection**.

When enabled, an issue that fails the shape gate at sprint entry gets an inline
remediation pass instead of being dropped immediately. Each time it fires, the
daemon emits a WARNING naming `forge groom` as the intended path:

```
[forge] Inline intake remediation ran at sprint entry for #1497.
[forge] Intended workflow: run `forge groom 1497` before sprint selection.
```

Treat this as training wheels for the operator who skipped pre-sprint grooming
(e.g. incident-time pressure) — not as a reason to skip it routinely. Every
firing is also recorded in the SQLite audit substrate
(`inline_remediation_events`) with the issue id, sprint id, triggering
shape-gate verdict, action taken, success, and the time/cost spent, so the
remediation-to-runnable cost ratio is queryable per milestone.

| Key | Default | Meaning |
|-----|---------|---------|
| `intake.grooming` | `false` | Enable the opt-in inline shape/grooming repair pass at sprint entry. |
| `intake.auto_fix` | `false` | Allow a single agent rewrite pass when the gate fails. |
| `intake.auto_fix_mode` | `comment` | `comment` posts the candidate and drops the story; `edit` rewrites the issue body in place and reruns the gate once. |

Canonical design: **ADR-0001 — Intake Readiness Workflow**

### Dev P2 policy (`dev.p2_policy`)

Controls how the dev agent treats P2 review findings during the current run.

| Value | Behavior |
|-------|----------|
| `in_scope` | Default. Fix P2s that touch the code being modified, or adjacent code relevant to that change, in the same run. |
| `all` | Fix every open P2 the dev agent encounters in the repo during the run. |
| `p1_only` | Legacy behavior. Only P1s are required unless a P2 must be fixed to complete the story safely or avoid a regression. |
(`docs/adr/0001-intake-readiness-workflow.md`), "Inline intake remediation
posture". `forge init` and generated templates emit no `intake.grooming` line
(so it resolves to `false`); there is no migration path.

### Sandbox capability profiles (`sandbox.capability_profile`)

The dev agent runs under a mechanical write-containment sandbox: writes are
confined to the story worktree plus a fixed allow-set. Some stacks cannot
*develop* a change inside that boundary — the iOS/Xcode toolchain, for example,
needs writes under `~/Library/Developer` and mach services for the simulator, so
`xcodegen`/`xcodebuild` fail with `Operation not permitted` and the agent cannot
build well enough to verify its own work.

`sandbox.capability_profile` names a **forge-owned preset** that widens the
sandbox by a bounded, declared amount:

```yaml
sandbox:
  capability_profile: xcode
```

| Preset | Platform | Grants |
|--------|----------|--------|
| `xcode` | macOS only | Xcode/SwiftPM state roots (`~/Library/Developer`, DerivedData caches, `/private/var/folders`) and the CoreSimulator / launch-services mach services. |

Rules that make this safe to adopt:

- **Presets are forge-owned.** A project selects one *by name*. It cannot
  author a preset, extend one, or override its contents; there is no inline
  `write_roots`/`mach_services` key, and supplying one is a config error.
- **Widening is always bounded.** No value disables the sandbox or grants
  `allow default`. The granted set is exactly the preset's declared list — a
  write to an out-of-worktree path the preset does not declare still fails.
- **Unknown names fail at config load**, so a typo cannot silently fall back to
  default containment.
- **Unexpressible presets fail closed.** Declaring `xcode` on Linux refuses the
  run with a clear reason (bwrap has no mach-service axis) rather than running
  with the declared capability missing.
- **Grants are audited.** The resolved profile name, write roots, and mach
  services are recorded in the run audit record under `workspace` and per dev
  iteration. A run with no preset records an explicit null profile with empty
  grants, so default containment is distinguishable from missing audit data.

**To adopt a preset**, an operator adds the two-line `sandbox` block above to
`forge.yaml` and re-runs. There is no default and no auto-detection: a project
that says nothing keeps today's containment exactly. To inspect what a preset
grants without running an agent — on any host, whether or not the toolchain is
installed:

```bash
python -c "from theforge.config.sandbox_capabilities import resolve_capabilities; \
print(resolve_capabilities('xcode').audit_payload())"
```

---

## Brief file (for `forge ideate`)

A plain text or markdown problem description used as input to multi-LLM
deliberation. No required structure — just describe the problem.

### Template

```markdown
# Brief: [Feature Name]

## Background

What exists today and why it's insufficient.

## What we need

The capability or behavior we want. Be specific about outcomes,
not implementation.

## Constraints

- Must work with existing X
- Cannot break Y
- Budget/timeline concerns

## Open questions

- Should we do A or B?
- Is C in scope?
```

### Usage

```bash
forge ideate briefs/my-feature.md --output stories/my-feature.md
```

---

## Secrets file (`.forge/.env`)

Standard dotenv format. Created by `forge secrets-init`.

If you still have a legacy `.forge/secrets.yaml`, migrate those values into
`.forge/.env`.

```bash
# .forge/.env — project-scoped secrets (gitignored)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...
GOOGLE_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
NTFY_URL=https://ntfy.sh/your-topic
```

---

## Review output (produced by reviewers, not user input)

For reference — this is the schema that reviewers must produce. You don't write
this, but understanding it helps interpret audit output.

```yaml
verdict: APPROVE | REQUEST_CHANGES
summary: "One-line description of findings"
findings:
  - severity: P1 | P2         # P1 = blocker, P2 = advisory
    file: "src/foo.py"
    line: 42
    description: "What is wrong"
    suggestion: "How to fix it"
story_compliance:
  matches_spec: true | false
  mismatches: []
test_coverage:
  adequate: true | false
  gaps: []
```

**Schema enforcement rules:**
- `APPROVE` with any P1 → overridden to `REQUEST_CHANGES`
- `REQUEST_CHANGES` with no P1 → schema error
- Invalid YAML → treated as `REQUEST_CHANGES`
- `spec_compliance` is accepted as a backward-compatible alias, but new
  reviewers should emit `story_compliance`.

---

## See also

- [Authoring Guide](authoring.md) — how to write a good issue or story by use case (feature, bug, refactor, rollup, docs/chore)
- [Getting Started](getting-started.md) — full setup walkthrough including config examples
- [CLI Reference](cli-reference.md) — all commands and flags
- [Provider Setup Guide](choose-your-provider-setup.md) — forge.yaml profiles for different scenarios
- [Troubleshooting](troubleshooting.md) — common errors and fixes
