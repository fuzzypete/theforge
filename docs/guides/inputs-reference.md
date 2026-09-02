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
| `name` | No | title-cased file stem | Human-readable title. Shows in logs and audit. |
| `slug` | No | file stem | Branch name (`forge/{slug}`), worktree path. Lowercase-with-dashes. |
| `test_target` | No | `.` | Stack-neutral test target substituted for `{test_target}` in the gate command. |
| `gate` | No | project default | Override gate: `"none"` skips the gate; any other string is executed verbatim as the gate command. |
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
stories:
  - stories/story-one.md           # local story file
  - stories/story-two.md
  - {issue: 123}                   # pull from GitHub issue #123
  - {issue: 124, slug: my-feature, depends_on: [story-one]}
```

> **Note:** `specs:` is a deprecated alias for `stories:` and still works.
>
> **Note:** auto-merge is a CLI decision: pass `--auto-merge` to `forge sprint`
> (it is not a manifest key).

### Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Human-readable sprint name |
| `budget_usd` | Yes | — | Total budget ceiling across all stories |
| `stories` | Yes | — | Ordered list of stories — local file paths or `{issue: N}` dicts |
| `max_parallel` | No | unset | Parallel story workers for this sprint. Integer >= 1; unset falls back to `sprint.max_parallel` in forge.yaml (default 1). |
| `worker_timeout_seconds` | No | unset | Per-worker timeout. Integer >= 1; unset falls back to `sprint.worker_timeout_seconds` in forge.yaml (default 3600). |

**What the worker timeout bounds — and what it excludes.** The resolved
per-story worker timeout is the *enclosing ceiling* for everything the story
does, and allowances inside it are derived against it rather than independently:

- The per-invocation development timeout is capped to fit the ceiling across the
  development cycles the story may run, and is further clamped at dispatch to the
  working time the story actually has left. A development invocation therefore
  ends on its own recorded, costed timeout instead of being killed by the
  scheduler mid-edit with no cost measured.
- Operator gate waits (human review, plan review, escalate) are bounded by the
  story's remaining window, so a gate never asks for a decision the story could
  not act on — and the time spent waiting is **excluded** from the worker
  timeout. Waiting for a human is not the worker being unresponsive, so the
  deadline is extended by exactly the length of the wait.
- A story whose deadline does elapse is recorded as an abnormal *timeout*
  (`abnormal_termination.kind: worker_timeout`), which is deliberately distinct
  from a review or quality failure: it says the story ran out of wall clock, not
  that its work was judged unacceptable.

### Cost governance vs. per-story estimates (converged model)

TheForge has one converged budget view with two distinct roles for dollar
values. Keeping them straight explains why a sprint stops but an individual
story never gets blocked for going "over budget."

- **Sprint-level governance (enforced).** The sprint `budget_usd` is the *only*
  post-hoc dollar enforcement. When cumulative sprint spend reaches it, no new
  stories launch **and any story already running is cancelled at its next
  coordinator phase boundary** — the sprint stops rather than finishing a story
  it can no longer afford. This is the surviving cost-governance surface.

  A cancelled story is recorded as *skipped*, with a reason naming the budget:
  the sprint stopped it, nothing judged its work, and re-running it under a
  larger cap is the whole remedy.

  Enforcement is a floor, not a guarantee of the exact figure: the phase that
  was already running when the cap was met still finishes, so a run can land
  slightly past its budget. Where it does, `forge sprint-status` marks the
  overrun beside the cost and budget, and `budget_status` /
  `budget_overrun_usd` record it in `sprint-summary.yaml` and the sprint audit.
  Spend that could not be measured is a separate case: the sprint will still
  cancel a running story if the measured lower bound already exhausts the cap,
  but it will not cancel that story merely because the remaining spend is
  unknown. In that unverifiable-only case, it refuses further dispatch instead.

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

### Reasoning effort (`assignment.reasoning_effort`)

The complexity score also selects a **reasoning-effort level per phase** — plan
`medium`/dev `low`/review `low` for scores 1–3, plan `high`/dev `medium`/review
`medium` for 4–6, `high` everywhere for 7–10. On transports that express
thinking as a token count instead, each level resolves to a configurable budget
(default `low` → 2048, `medium` → 8192, `high` → 24576). Transports with no
passthrough leave the value unapplied and record `provider_unsupported`.

Everything is overridable sprint-wide or per provider, and validated at load:

```yaml
assignment:
  reasoning_effort:
    enabled: true                # false leaves the axis flat (still recorded)
    phases:
      dev:
        - {max_score: 5, effort: low}
        - {max_score: 10, effort: high}
    token_budgets: {low: 2048, medium: 8192, high: 24576}
    providers:
      google:
        token_budgets: {high: 32768}
```

See [Routing policy](routing-policy.md#reasoning-effort-per-phase) for the full
table, the provider-support matrix, and the `routing_decision` shape.

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

### Batch groups (cost-aware, scheduler-decided, opt-in)

Forge has **three** scheduling primitives, and they answer different questions:

| Primitive | Question it answers | Grouping signal |
|-----------|--------------------|-----------------|
| Dependencies / DAG | Which stories must run *before* others? | `depends_on`, collision edges |
| Conflict bundles | Which stories overlap enough that implementing them apart causes merge pain? | shared `Area:` / `likely_files` |
| **Batch groups** | Which small *independent* stories can share one dev assignment to cut per-story overhead? | **absence** of overlap |

A batch group packs several small, independent stories into a single dev
assignment. It exists for cost and throughput, not for collision avoidance —
so where bundling requires evidence of overlap, batching requires evidence of
*independence*.

Batching is **off by default**. It is not automatically cheaper: a combined
prompt can exceed what one dev agent holds well, tests can broaden, and review
can get harder. Enable it deliberately:

```yaml
sprint:
  batch:
    max_stories: 2            # 1 (default) disables batching entirely
    max_complexity_budget: 2  # summed complexity weight across the group
    max_touched_files: 6      # cap on the group's combined likely_files
```

A story is batch-eligible only when **all** of its preflight evidence is
present and bounded:

- `complexity == small`
- `work_type` in `{bug, mechanical}`
- `sufficiency == implementation_ready`
- `likely_files` is known, non-empty, and within `max_touched_files`

and a *group* forms only when, in addition:

- no member touches a dependency edge in either direction — not its own
  `depends_on`/collision edges, and nothing else depends on it;
- no member was already claimed by a conflict bundle (bundling wins when both
  would apply);
- members are pairwise non-overlapping under the bundling predicate (no shared
  `Area:`, no `likely_files` intersection) — otherwise it is a bundle question,
  not a cost question;
- the group's summed complexity and combined footprint stay under the configured
  budgets.

Anything missing fails closed: an unknown footprint or a missing preflight never
batches. Batch groups therefore never override dependencies or conflict bundles.

**What is shared, and what is not.** Only the DEV assignment is shared: one
worktree, one branch, one dev agent, one prompt carrying every member's spec and
requiring per-story completion notes. Everything else stays per story — each
member gets its own review against its own spec, its own findings, its own cost,
its own outcome, and its own audit record.

**Landing is shared, and reported as shared.** The group's commits live on one
branch — the leader's — so a member is exactly as landed as its leader is, and
no more. Whenever the leader's landing reaches a terminal answer, that answer is
propagated to every member: if the branch lands, members are recorded DONE and
landed; if it fails to land, members are recorded `MERGE_FAILED` naming the
leader, because their changes are not on the base branch. This holds for a
landing that resolves *late* — an auto-merge PR that only reports MERGED or
closed during queued-PR polling or sprint wrap-up, long after the member rows
were written. A member that failed on its own merits keeps its own verdict; a
leader landing successfully does not retroactively approve it.

`forge status` renders batch groups distinctly from conflict bundles:

```
[bundle: issue-41  issue-42]
    ✓ issue-41  ...
    ✓ issue-42  ...

[batch: batch-issue-10  issue-10  issue-11]
    ✓ issue-10  ...
    ✓ issue-11  ...
```

The `batch_group` field in per-story audit dumps and live state is
scheduler-written, like `bundle_candidate`: it records the group the scheduler
packed the story into, and is `null` for a story dispatched on its own.

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
  - anthropic/sonnet/cli
  - anthropic/opus/cli

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

### Model identity and transport

A model is identified by exactly three things: **provider**, **model**, and
**transport kind** (`cli` or `api`). Runner/executor is derived from
`(provider, transport.kind)` — `(openai, cli)` is the Codex CLI, `(anthropic,
cli)` is the Claude CLI, `(google, api)` is the Google API adapter — so config
normally does not name a runner. The one exception is a `(provider, kind)` pair
that has more than one executor, where `transport.runner` picks between them; a
runner that contradicts the pair is rejected at load.

The mapping form spells the same identity out, and is the place to attach
endpoint and routing metadata:

```yaml
models:
  enabled:
    - provider: openai
      model: gpt-5.5
      transport:
        kind: cli
      routing:
        tier: strong

    - provider: openai
      model: gpt-5.5
      transport:
        kind: api                  # same provider + model, different identity
      routing:
        tier: strong

    # A locally served OpenAI-compatible model. Locality is endpoint metadata:
    # there is no `local/` provider and no `local` transport kind.
    - provider: openai
      model: qwen2.5-coder:32b
      transport:
        kind: api
      base_url: http://localhost:11434/v1
      routing:
        tier: fast
```

`routing` (tier, capability, cost rank, dev capability, phase eligibility) is
selection *policy*, not identity — two entries differing only in `routing` are
the same model.

### Model definitions

That mapping is the **canonical model definition schema**, and it is the only
one. The models TheForge ships with are written in it too: they live in
`src/theforge/config/data/models.yaml` inside the package and are read by the
same parser that reads your `forge.yaml`, so a project-declared model is not a
second-class one — anything the shipped set can express, yours can:

```yaml
models:
  enabled:
    - provider: google              # adapter family (required)
      model: gemini-4-pro           # model id passed to that adapter (required)
      transport:
        kind: api                   # cli | api (required)
        # runner: ghaw              # only for (provider, kind) tuples with
                                    # more than one executor
      base_url: https://…/v1        # endpoint metadata (local/compatible servers)
      routing:
        tier: strong                # cheap | fast | strong
        capability: 10              # 1–10 relative capability
        cost_rank: 3                # 1=cheap, 2=moderate, 3=expensive
        dev_capable: false          # may this model own the dev role?
        phase_eligibility: [dev, plan, review]
        cost_rank_basis: declared-policy
      cost:
        input_per_mtok: 2.00
        output_per_mtok: 12.00
        cached_input_per_mtok: 0.20     # only if the provider bills its cache
                                        # tier at its own published rate
        pricing_provenance: gemini-4-pro-2026-08
        rate_basis: token_rates         # or `provider_reported`, with no figures
      identity:
        status: served                  # served | retired
        verified_against: provider model list
        verified_on: 2026-08-10
      invocation:
        reasoning_mode: enabled         # enabled | disabled
```

**Adding a model needs no code and no release** as long as it runs on an adapter
that already exists (`anthropic`, `openai`, `google`, `deepseek`). Adding a
*provider* does need code, because a provider needs a runner module — declaring
one that has no adapter fails at load with a message naming the adapters that
do exist.

**Cost attribution.** `pricing_provenance` names the concrete billed identity
the figures were recorded for. Omit it and the figures are *unattributed*:
carried for reference, ignored by cost banding and price tie-breaks. Write
`pricing_provenance: null` explicitly for a vendor shorthand whose price you
record but cannot vouch for. `cost_rank_basis` states why the band holds when it
is not simply this entry's own attributable price band; a band that is neither
price-attributable nor explained is a load error rather than a silent guess.

`cached_input_per_mtok` is for providers that publish an independent rate for a
prompt-cache hit rather than expressing it as a fraction of the uncached rate.
Omit it and forge applies its generic 10%-of-input discount, which is what
OpenAI and Anthropic actually bill.

**A rate is declared here and nowhere else.** The catalog entry (shipped or
yours) is the only place a per-MTok figure comes from — there is no packaged
rate table behind it that a release could change and your configuration could
not (#2388; `docs/reference/dropped-legacy-rates.md` records the one that used
to exist). So an entry with no figures is an entry that cannot be priced, and
`rate_basis` is how you say which kind of "no figures" you mean:

- `token_rates` (the default) — spend is token count × the rates on this entry.
  Declaring no figures under this basis means the identity records its cost as
  unknown, and `forge check-config` says so at load.
- `provider_reported` — the transport returns the figure it was billed (the
  Claude CLI does), so no rate card is ever consulted and none is missing. An
  entry declaring this may not also declare per-MTok figures: a rate nothing
  reads is a rate nothing keeps current.

**Upstream identifiers.** `identity` is what the entry claims about the name the
*provider* serves, and when that claim was last checked. It matters because a
retired identifier often keeps resolving: the call succeeds and returns real
token counts while the capability, tier and price recorded here describe a model
that is no longer behind the name.

- `status: retired` makes the entry unroutable. It is not deleted, so a
  configuration still naming the identifier is told what the provider did with
  it — `retired_reason` is required and should name the replacement.
- `verified_on` / `verified_against` record a check. They expire: past the window
  in `config/model_identity.py` the entry reverts to *unconfirmed*, the same
  state an entry that never declared a check is in. Unconfirmed is not an error —
  `forge check-config` reports it under `upstream identifier not confirmed` so
  you see it before a run spends against it.
- A retired identity is **reserved**. `models.custom` and inline `models.enabled`
  mappings cannot redeclare one with their own routing and cost: the retirement
  is a property of the identifier, not of the file it was declared in.
- An API cost derived from a rate card whose identity is unconfirmed is recorded
  as `cost_provenance: estimated_unconfirmed` rather than `estimated`, so a
  decision made on price can tell how much the price is worth.

**Request-level modes.** `invocation.reasoning_mode` states a behavioural mode
the provider expresses as a *request parameter* rather than as a distinct model
name (DeepSeek's `thinking` block, for one). Where a provider works that way, an
entry banded as a reasoning model has to declare it — otherwise the band
describes a mode no invocation ever asks for.

**Aliases and versions are both valid model strings.** A `model:` may name a
vendor *family alias* (`opus`, `sonnet`, `haiku` — the Claude CLI shorthands) or
a *concrete version* (`claude-opus-4-6`). Both ship in the catalog, side by
side, as separate identities:

```yaml
models:
  enabled:
    - anthropic/opus/cli               # whatever Anthropic currently ships
    - anthropic/claude-opus-4-6/cli    # one specific model, forever
```

They mean different things and neither replaces the other:

- An **alias** tracks the family's current release. It is a reasonable default
  and is why an alias-only configuration keeps working and keeps getting the
  newest version with no edit. Its identity can move, so its price literal
  carries no `pricing_provenance` and its band states a `cost_rank_basis`.
- A **version** is the identity the vendor bills under. It never moves, its
  figures are attributable, and — the reason it exists as a candidate — an
  *earlier* generation can be named and offered as a cheaper alternative, which
  an alias by construction cannot express.

**A recorded run distinguishes what was configured from what served.** When an
alias is selected, the vendor picks the version at invocation time. Every
recorded invocation stores both identities separately (`ledger.configured_identity`
and `ledger.resolved_primary_identity` in the audit record; the
`invocation_identities` table in the audit substrate), so:

- profile evidence gathered under an alias is attributable to the versions that
  produced it — every routing signal reports a `resolved_population` saying
  which concrete models the population describes and whether it is `mixed`;
- a change in what an alias resolves to is queryable rather than surprising:
  `forge audits alias-drift` groups recorded invocations by configured identity
  and lists the versions each resolved to over time, `--changed-only` for just
  the ones that moved.

A served version the catalog has not pinned is recorded verbatim and reported
`unresolved` rather than folded into the family alias — "the alias moved to
something we do not have an entry for" is a fact worth surfacing.

**Overlaying a shipped definition.** A declaration whose identity matches a
shipped one refines it: fields you state win, fields you omit keep the shipped
value. `forge check-config` reports which source supplied each field.

**Where a definition can go.** Either surface accepts the canonical schema:
inline in `models.enabled` (defines and selects in one place), or under
`models.custom` (a reusable declaration, selected by its key). They are the same
shape and the same parser:

```yaml
models:
  enabled: [anthropic/sonnet/cli, fast-reviewer]
  custom:
    fast-reviewer:                  # operator-chosen key — the selector
      provider: google
      model: gemini-4-flash
      transport: {kind: api}
      routing:
        tier: cheap
        capability: 7
        phase_eligibility: [review]
      cost:
        input_per_mtok: 0.30
        output_per_mtok: 2.50
```

A `models.custom` declaration stands alone: it does not inherit from a shipped
entry, and replacing a shipped identity requires `override: true` alongside the
definition.

**A declared price is the price that gets recorded.** The `cost:` figures are
read once, at load, into a rate registry keyed by
`(provider, model, transport.kind)`, and every run prices its tokens from the
identity it actually dispatched on. Declaring a price is all that is needed for
that model's spend to be measured — no code change, no second table to update.

Two consequences worth knowing:

- **A price belongs to one transport.** `openai/gpt-5.5/cli` and
  `openai/gpt-5.5/api` are different identities and are priced separately. If
  you price one and dispatch on the other, the unpriced one records cost as
  unknown — it does not borrow its sibling's rate, because the two genuinely
  bill differently often enough that guessing is worse than saying nothing.
- **You are told before you spend.** Any model the configuration can dispatch
  on — seated profile, adaptive-pool candidate, `fallback_models` entry, or
  `api_fallback` target — that cannot be priced is warned about when the config
  loads, naming the paths it is reachable on. `forge check-config` shows these
  in its WARNINGS section. The load does not fail: an unpriced model still runs
  and records its cost as unknown.
- **A `fallback_models` entry is checked as an API identity, even on a CLI
  profile.** That is where it actually runs: the failures that trigger a model
  fallback are quota exhaustion and model-not-found, which the CLI that just
  refused would only reproduce, so forge retries through the provider's API
  adapter. Price the fallback on `<provider>/<model>/api`, not on the CLI
  identity the primary uses.

Transports that report their own spend (the Claude CLI's billed total, gh-aw's
AI-credit accounting) need no `cost:` block and are never warned about.

**Existing configuration keeps loading.** The flat `models.custom` form below
and inline `models.enabled` mappings written before this schema existed are
translated into it at the parse boundary — adopting the canonical shape is
optional, not forced:

```yaml
models:
  enabled: [anthropic/sonnet/cli, gpt-5.5]   # a custom declaration may still be
  custom:                                     # selected by its declaration key
    gpt-5.5:
      provider: openai                        # alias tokens (openai-api,
      model: gpt-5.5                          # gemini-cli) still normalize
      tier: strong
      input_cost_per_mtok: 1.50
      output_cost_per_mtok: 12.00
```

The flat form still derives `capability` from `tier` and cannot set
`phase_eligibility`; use the canonical shape when you need those. A declaration
that mixes the two — flat `tier` next to a `routing:` block — is rejected rather
than resolved under one reading.

**Rejected/migration-only spellings.** `openai-api/gpt-5.4`,
`gemini-cli/gemini-2.5-pro` and `claude/opus` are legacy aliases. They still
load, but they are rewritten to their canonical identity at the parse boundary
and never appear in loaded config, telemetry, or audit records. Write the
canonical form.

### Config reference (common surface)

This block covers the commonly used surface, not every key `forge.yaml`
loading supports — `src/theforge/config/` (`load.py` and `types.py`) is
authoritative.

```yaml
project: my-project                # project name for logging/audit

# ── Model list and derived roles ──────────────────────────
# A model identity is provider + model + transport kind, written
# <provider>/<model>/<cli|api>. Transport is never encoded in the provider
# token: openai-api/... and gemini-cli/... are rejected spellings, accepted only
# as migration aliases at the parse boundary.
models:
  - anthropic/sonnet/cli
  - anthropic/opus/cli
  - openai/gpt-5.4/cli             # Codex CLI
  - openai/gpt-5.4/api             # same model, OpenAI API adapter — a distinct identity

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
  setup_timeout: 120                   # seconds; default baseline before sprint
                                      # host-load scaling widens it
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
  auto_push: false                    # push base_branch to origin after successful auto-merge;
                                      # required true when on_approve is "merge-pr"
  stale_worktree_days: 1              # remove leftover worktrees older than N days; 0 = always remove
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
  dev_verification_commands: {}        # optional named commands the coordinator runs OUTSIDE the dev
                                       # sandbox when the dev agent requests them by name. See
                                       # "Dev verification commands" below. Default: none offered.
  dev_verification_max_requests: 10    # fail-closed per-iteration request budget (counts refusals too)
  profiles: {}                         # optional named validation profiles; exactly one carries
                                       # merge authority. Omit entirely to keep the
                                       # gate_command/test_command behaviour above. See
                                       # "Validation profiles" below.

# ── Retry policy ───────────────────────────────────────────
retry:
  max_dev_iterations: 3      # dev attempts within one review cycle
  max_review_cycles: 2       # full dev→review loops before ESCALATE. A blocking
                             # finding the coordinator raises itself in VALIDATE
                             # (gate failure, hard convention violation) also
                             # spends one, once the dev attempts above are gone.
                             # This is a CEILING, not a quota: a review loop that
                             # is walking a topology — each cycle resolving its
                             # predecessor's finding and raising the same concern
                             # at a new location — escalates at the third such
                             # cycle, before the ceiling, so the operator decision
                             # is about the story's framing rather than about the
                             # latest finding. Detection is deterministic and
                             # conservative (any ambiguity spends another cycle);
                             # the evidence is recorded under the audit record's
                             # `review_topology_signal` key.
  max_review_parse_retries: 2  # reviewer output parse/schema error retries
  max_diagnose_parse_retries: 2 # `forge diagnose` reformat-only retries when the
                             # investigative agent finishes but emits unparseable
                             # YAML. The retry re-serializes the completed
                             # investigation; it does NOT re-investigate. 0 disables.
  max_plan_regen_attempts: 3 # plan review reject → regeneration cycles
  plan_escalation_threshold: 2 # consecutive plan rejections before the planner
                             # model is escalated to a stronger one
  max_dev_transport_retries: 1 # per-iteration retries on transient dev
                             # transport/provider failure
  max_spec_gap_pauses: 1     # specification-gap pauses a run may open. A dev
                             # agent that hits an underspecified acceptance
                             # criterion emits <forge_spec_gap>; the run pauses
                             # for an operator answer (`forge decide <run-id>
                             # "<answer>"`) instead of guessing. Past the
                             # allowance — or when the pause expires — the run
                             # proceeds under the assumption the agent recorded,
                             # and the audit says which happened. 0 disables.
  preflight_complexity_gate_threshold: 9  # a PROCEED story whose preflight
                             # complexity score reaches this pauses at the end of
                             # PREFLIGHT and asks whether to plan it as scoped
                             # (`forge decide <story-run-id> approve`) or return
                             # it to be split (`… decompose`) — before any later
                             # phase is charged. Active by default. There is no
                             # enable switch: a threshold above 10, the highest
                             # score preflight can assign, disables the gate.
  preflight_complexity_gate_no_decision: decompose  # what an EXPIRED gate does.
                             # Only the two actions an operator may pick are
                             # accepted. Anything else — absent, empty,
                             # misspelled — returns the story rather than
                             # proceeding, and the run records that a fallback
                             # was applied, so no misconfiguration can spend on a
                             # story nobody approved.
  adaptive_iterations: true  # scale per-story iteration limits from preflight
                             # complexity; the max_* fields above act as the floor
  max_dev_iterations_cap: 0  # hard ceiling for adaptive growth; 0 = same as
  max_review_cycles_cap: 0   # floor (no growth). Set explicitly to opt in.
  escalate_policy: prompt    # "prompt" | "auto_approve" | "reject" — how an
                             # escalation is decided while it is open
  escalate_timeout_policy: preserve  # what an escalate gate that EXPIRES means.
                             # "preserve" (default) keeps the pending checkpoint
                             # and waits for an operator — unchanged behaviour.
                             # "apply_advice" applies the escalation advisor's
                             # recommendation as if an operator had selected it,
                             # for unattended overnight runs. A recommendation of
                             # `elevate`, an unusable/absent report, or one this
                             # run cannot perform still preserves the story — no
                             # fallback action is ever substituted. A selection
                             # that arrives before expiry always governs.
  # More retry knobs exist (plan/plan-review/review transport retries, quorum,
  # degrade policy, …) — see RetryPolicy in src/theforge/config/types.py.

# ── CLI→API transport fallback, keyed by provider family ──
# The provider never changes across a fallback — only the transport does. When
# a CLI transport fails in a retryable way (quota exhaustion, model-not-found),
# the run continues on the same provider's API transport with the model named
# here.
auto_transport_fallback: true      # derive fallbacks automatically; explicit
                                   # entries below win over derived ones
transport_fallback:
  openai:
    model: gpt-5.4
    timeout_seconds: 600

# ── Sprint defaults ────────────────────────────────────────
sprint:
  max_parallel: 1                  # parallel story workers (manifest key wins)
  worker_timeout_seconds: 3600     # per-worker timeout (manifest key wins)
  post_sprint_triage: false        # opt-in headless `forge triage` proposal pass
                                   # after the sprint's terminal result. Proposes
                                   # and persists a pending operator decision;
                                   # never ratifies, never mutates a tracker, and
                                   # a failure never fails the sprint. See
                                   # docs/guides/cli-reference.md "Headless mode".
  # batch: — see "Batch groups" above

# ── Stuck-agent detection (dev phase only) ─────────────────
stuck_detection:
  enabled: true
  no_progress_iterations: 5        # iterations without file modification → stuck
  repeat_threshold: 4              # consecutive identical tool calls → stuck
  error_threshold: 4               # consecutive identical error results → stuck
  post_nudge_iterations: 3         # iterations after the nudge before termination

# ── forge diagnose (separate budget from the sprint pipeline) ──
diagnose:
  output_destination: body_section # "body_section" | "comment" | "pr_to_body"
  budget_usd: 1.50
  timeout_seconds: 600
  autonomous_default: true         # default mode when --interactive is not passed

# ── Lifecycle hooks ────────────────────────────────────────
hooks:
  pre_run: .forge/hooks/pre_run.sh    # each key optional; commands run at the
  post_run: .forge/hooks/post_run.sh  # named forge event
  post_merge: .forge/hooks/post_merge.sh
  post_sprint: .forge/hooks/post_sprint.sh
  timeout_seconds: 30

# ── Conventions ────────────────────────────────────────────
conventions:
  hard:                            # mechanically enforced; omit section = no checks
    max_module_lines: 500          # ratcheted: a module already over this at the
                                   # branch point may not grow (it may shrink
                                   # freely); one within it is refused for
                                   # crossing it — see docs/adr/0008
    max_test_file_lines: 1000      # advisory only
    no_circular_imports: true
    test_mirrors_source: true
    no_scratch_files: true
    stack: python                  # string or list; enables stack-specific root-file rules
  soft:                            # list of prose rules injected into agent prompts
    - "Coordinator is pure Python — no LLM calls for routing decisions"
  advisory:                        # aggregation of non-blocking convention debt
    artifact_path: ".forge/conventions/advisory.yaml"
    summary_top_n: 10
    noteworthy_threshold_percent: 10.0
    commit_shared_artifact: false
    issue_filing:
      enabled: false

# ── Classic manual profiles (LEGACY — mutually exclusive with models:) ─────
# This path predates first-class transport: it spells dispatch as a pair of
# sibling cli/provider fields with no transport object, so a profile's identity
# is not the canonical (provider, model, transport.kind) tuple. It still loads —
# the pair is normalized into a transport at parse time — but it is legacy.
# Prefer models: + overrides: above. Omit models: entirely when using it.
profiles:
  # Dev agent (implements the story)
  dev:
    cli: claude                # legacy CLI spelling: "claude", "codex", "gemini"
    # provider: openai         # legacy API spelling: "openai", "anthropic", …
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

# ── Post-run knowledge capture (optional) ─────────────────
# Defaults to disabled. See "Post-run knowledge summaries" below.
knowledge:
  run_summaries: false        # emit an evidence-backed summary after a run reaches DONE

# ── Sandbox capabilities (optional) ───────────────────────
# Omit entirely for default write containment. See "Sandbox capability
# profiles" below.
sandbox:
  capability_profile: xcode   # forge-owned preset name; omit for the default
  write_roots:                # project grants ADDED to the preset's (optional)
    - ~/Library/Preferences
  mach_services:              # macOS only; refused on other backends (optional)
    - com.example.toolchaind

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

### Post-run knowledge summaries (`knowledge.run_summaries`)

Disabled by default. When enabled, a run that reaches DONE is distilled once
into `.forge/knowledge/summaries/{run_id}.yaml` — what changed, what was
learned, review insights, and complexity signals — with a
`authoritative_run_record` backlink to `.forge/audits/runs/{run_id}.json`. The
run record carries no forward pointer; a run's summary is found by that path
convention.

Two properties are worth knowing before enabling it:

- **It is not load-bearing.** Generation happens after the DONE transition and
  after the audit record is written. A summary that fails to generate, parse, or
  validate leaves the run's outcome and audit trail unchanged.
- **Claims must cite the run.** Every `what_was_learned` entry must cite a
  finding id, plan step id, review cycle, changed-file path, or diff ref that
  actually exists in that run's audit record. A citation that does not resolve
  rejects the whole summary rather than persisting an unverifiable claim.

Generation dispatches a bounded, tool-free agent over an API transport.
`knowledge.ref` chooses the summary model explicitly; when omitted, summaries
inherit `plan.ref` only if planning already dispatches over API transport. A
CLI plan model with no explicit `knowledge.ref` skips generation with a
warning. `transport_fallback` does not choose the durable-knowledge author. Its
cost is recorded on the artifact under `generation.cost_usd`, because it is
spent after the run's own cost accounting has closed.

`knowledge.prior_run_context` enables Layer 3 consumption: context assembly may
then offer indexed prior summaries to the **plan, dev, and review** phases as
advisory, droppable context items, and to **preflight** only as audit-derived
signal renderings with no summary prose. Preflight's output still drives
coordinator control flow (ADR-0002 clause 5), so only bounded mechanical signals
may appear there. A summary is offered only when the knowledge index carries an
*admissible* verdict for it — a summary with an inadmissible verdict, or with no
verdict at all, is never injected. Relevance is scored from deterministic index
fields only (file overlap, domain, story shape, indexed patterns, recency), so
summary prose can never select itself in.

Every decision is recorded per phase in the audit record under
`context_manifests[].prior_run_context` — what was included and why, what was
dropped, and a `note` that distinguishes:

- a missing index ("missing or was never built"),
- an unreadable index (parse failure or malformed `entries` payload),
- an unsupported index schema version,
- an empty but healthy index ("no indexed summaries"),
- no relevant prior knowledge for this story,
- and prior knowledge that existed but was withheld as inadmissible or stale.

The prior-run index is a derived artifact built by `forge index`; the selector
never builds it on demand. If the manifest reports missing, unreadable, or
unsupported-schema index state, rebuild `.forge/knowledge/index.yaml` with
`forge index`. Design doctrine lives in `docs/plans/knowledge-capture.md`.

`knowledge.invariant_context` (default `false`) gates the #1875 invariant-index
spike, and `knowledge.invariant_sources` lists the Markdown globs the extractor
scans — source locations only, never invariant prose. Project invariants are
marked in the project's own authoritative docs:

```yaml
knowledge:
  invariant_context: false
  invariant_sources: ["**/*.md"]   # default: every Markdown file; narrow if you like
```

```md
<!-- forge-invariant id="summaries-advisory" scope="area:audit phase:plan,dev,review files:src/theforge/knowledge_*.py" enforcement="review" -->
LLM-generated summaries advise agents; they never drive coordinator control flow.
<!-- /forge-invariant -->
```

`forge index --invariants` rebuilds the derived index at
`.forge/knowledge/invariants/index.yaml`, which stores provenance and
applicability metadata only — the source document stays authoritative and is
re-read when text is injected. When the gate is on, plan and dev may receive
narrow capsules while **review always receives the broader enclosing source
section**, and preflight receives nothing at all (ADR-0002 clause 5). When scope
confidence is low, the broader source is included rather than the rule dropped.
Every decision is recorded per phase under `context_manifests[].invariant_context`
with `included`, `dropped`, and `uncertain` entries. Design doctrine and the
adoption decision live in `docs/plans/1875-invariant-index-spike.md`.

### Adaptive assignment (`assignment:`)

Config mechanics for the v0.13 adaptive routing surface. Behavior and doctrine
live in [Adaptive assignment](adaptive-assignment.md) and
[Routing policy](routing-policy.md); source of truth is `AssignmentConfig` in
`src/theforge/config/types.py` and `_parse_assignment` in
`src/theforge/config/models.py`.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled` | bool | `false` | Master switch: derive and route roles adaptively from the `models:` pool. Load fails if true and no reviewer-eligible agent has working auth. |
| `min_reviewers` | int | `1` | Minimum reviewers per story (reviewer-count axis floor). |
| `max_reviewers` | int | `3` | Maximum reviewers per story (reviewer-count axis ceiling). |
| `prefer_cross_provider` | bool | `true` | Greedily pick reviewers from different providers when filling the panel. |
| `max_cost_per_story_usd` | float or null | `null` | Pre-run routing cost target: downgrades tiers so the estimated per-story routing cost stays under it. Not a spend cap — see "Cost governance" above. |
| `escalation_memory` | bool | `true` | Record per-story escalation outcomes (via the audit substrate) so later runs on the same story route from the escalated tier. |
| `adaptive_enabled` | bool | `true` | Allow historical performance signals to move routing. `false` = static band-only routing (complexity → tier table, no profile consultation). |
| `recency` | block | — | Recency weighting of historical outcomes — see [Recency weighting](#recency-weighting-assignmentrecency). |
| `exploration.explore_every_n` | int >= 1 | `5` | Per-routing-key challenger cadence: every Nth run for a key may race a challenger instead of the cached winner. |
| `exploration.min_sample_size` | int >= 1 | `3` | Admissible (non-tainted) runs required before a winner is declared for a routing key. |
| `exploration.per_sprint_cap` | int >= 0 | `1` | Max exploration runs per sprint across all routing keys; `0` disables exploration. |
| `exploration.reliability_floor` | float in [0,1] | `0.7` | Minimum recency-weighted success rate a candidate must clear to be *exploited* as a key's winner. Winner ranking is expected-cost-to-trusted-completion first (`avg_cost_usd / success_rate`), so this floor is what stops the cheapest-failing model from winning. Below the floor a candidate is excluded from exploitation no matter how cheap, but stays eligible for challenger exploration. |
| `exploration.challenger_rotation` | `least_sampled` or `random` | `least_sampled` | How the challenger is drawn from the eligible non-winner pool. `least_sampled` draws from the candidates with the fewest admissible runs for the key (RNG breaks ties), so every alternative gets a race within `pool - 1` cadence hits — the incumbent's history volume cannot make its own displacement impossible. `random` restores the uniform draw. |
| `exploration.performance_cache_path` | str | `.forge/performance_table.yaml` | Rebuildable, gitignored derived view of per-key aggregates, for operator inspection only — never read as authoritative. |
| `reviewer_completion_threshold` | float | `0.5` | A reviewer whose recency-weighted completion rate (returned a parseable verdict) falls below this sorts after higher-completion candidates. Sort-after, not filter-out. |
| `reviewer_completion_min_runs` | int | `5` | Attempts a reviewer must accumulate before completion sorting applies (cold-start protection). |
| `reviewer_value_enabled` | bool | `false` | Opt in to plan-reviewer P1-uniqueness routing: low-uniqueness reviewers sort after higher-value candidates. Strict bool — non-boolean values are a config error. |
| `reviewer_value_uniqueness_threshold` | float in [0,1] | `0.34` | Uniqueness rate (fraction of blocking findings no peer corroborated) below which a plan reviewer is deprioritized. |
| `reviewer_value_min_runs` | int >= 1 | `5` | Admissible P1-bearing samples required before plan-reviewer value reordering fires. |
| `code_review_value_enabled` | bool | `false` | Same mechanism for code-review reviewer selection, over its own independently accumulated profile section. Strict bool. |
| `code_review_value_uniqueness_threshold` | float in [0,1] | `0.34` | Code-review counterpart of the uniqueness threshold. |
| `code_review_value_min_runs` | int >= 1 | `5` | Code-review counterpart of the sample floor. |
| `dev_promotion_threshold` | float in [0,1] | `0.60` | Below this recency-weighted success rate at the story's complexity band, the dev tier is pre-promoted one step before the first iteration. |
| `dev_promotion_min_runs` | int >= 1 | `5` | Admissible (non-tainted) runs required before pre-promotion can fire; below the floor, routing falls through to the static tier. |
| `plan_tier_reduction` | bool | `true` | A clean plan review on a medium-complexity story may step the dev tier down one level (strong→mid, mid→cheap). `false` always honors the preflight-assigned tier. |
| `reasoning_effort` | block | — | Score-driven reasoning-effort overrides — see [Reasoning effort](#reasoning-effort-assignmentreasoning_effort). |

The value-routing and promotion fields validate strictly at load: thresholds
must be numbers in `[0.0, 1.0]`, min-runs must be integers at or above the
minimum, enable flags must be actual booleans. Out-of-range values are a config
error, never a silent clamp.

### Sandbox capability profiles (`sandbox`)

The dev agent runs under a mechanical write-containment sandbox: writes are
confined to the story worktree plus a fixed allow-set. Some stacks cannot
*develop* a change inside that boundary — the iOS/Xcode toolchain, for example,
needs writes under `~/Library/Developer` and mach services for the simulator, so
`xcodegen`/`xcodebuild` fail with `Operation not permitted` and the agent cannot
build well enough to verify its own work.

The `sandbox` block widens the sandbox by a bounded, declared amount. It has
three keys, all optional and all additive:

```yaml
sandbox:
  capability_profile: xcode        # a forge-owned preset, selected by name
  write_roots:                     # project grants, ADDED to the preset's
    - ~/Library/Preferences
    - /opt/toolchain
  mach_services:                   # macOS only
    - com.example.toolchaind
```

| Preset | Platform | Grants |
|--------|----------|--------|
| `xcode` | macOS only | Xcode/SwiftPM state roots (`~/Library/Developer`, DerivedData caches, `/private/var/folders`) and the CoreSimulator / launch-services mach services. |

`write_roots` and `mach_services` exist because the shipped presets cannot
anticipate every toolchain: what a real stack needs is not knowable in advance
from inside TheForge. They stand alone as well — a project may declare grants
with no `capability_profile` at all.

Rules that make this safe to adopt:

- **Project grants are additive, never subtractive.** Presets stay forge-owned:
  a project may add write roots and mach services alongside a preset, but it
  cannot author a preset, override one, or remove anything one grants. The
  applied set is the preset's list plus the project's, de-duplicated.
- **Widening is always bounded.** No key disables the sandbox or grants
  `allow default` — `allow_default`, `disabled`, and `mode` are config errors.
  The granted set is exactly the declared list; a write to an out-of-worktree
  path nobody declared still fails.
- **A grant may not be an escape.** A `write_roots` entry that resolves to `/`
  or to the invoking user's home directory is refused by name, rather than
  widened into a whole-filesystem grant.
- **Unknown preset names fail at config load**, and a malformed grant list (not
  a list of non-empty strings) is a load error too — a silently dropped grant
  is a capability the run believes it has and does not.
- **Unexpressible capabilities fail closed.** Declaring `xcode` — or any
  `mach_services` — on Linux refuses the run with a clear reason (bwrap has no
  mach-service axis) rather than running with the declared capability missing.
  The same applies per transport: a dev transport that does not apply forge's
  host sandbox (codex, gh-aw, the API tool runtime) refuses a run that declares
  capabilities, instead of dropping them silently. Use a transport that applies
  the host sandbox (claude, gemini) for a project that declares grants.
- **Grants are audited.** The resolved profile name, write roots, and mach
  services are recorded in the run audit record under `workspace` and per dev
  iteration, with project-declared grants listed separately under
  `project_write_roots`/`project_mach_services`. A run with no declaration
  records an explicit null profile with empty grants, so default containment is
  distinguishable from missing audit data. A refused declaration records zero
  grants plus `requested_*` and `unsupported_reason`, so a fail-closed run never
  audits as though the capability was applied.

**To adopt this**, an operator adds the `sandbox` block above to `forge.yaml`
and re-runs. There is no default and no auto-detection: a project that says
nothing keeps today's containment exactly. To inspect what a declaration grants
without running an agent — on any host, whether or not the toolchain is
installed:

```bash
python -c "from theforge.config.sandbox_capabilities import resolve_capabilities; \
print(resolve_capabilities('xcode', write_roots=('/opt/toolchain',)).audit_payload())"
```

### Validation profiles (`validation.profiles`)

`gate_command` and `test_command` are two fixed slots: one authoritative and
complete, one advisory with no stated relationship to it. Nothing in between can
be said — which checks are cheap, which result decides a merge, how a scoped run
differs from a complete one. Profiles let a project state it:

```yaml
validation:
  profiles:
    complete:
      command: "make gate"
      authority: merge
    fast: "make test-fast"
    targeted: "make test TARGET={test_target}"
```

- **Names are a closed set:** `complete`, `fast`, `targeted`. TheForge selects a
  profile by meaning, so a name it does not recognise would load and then never
  run; an unknown name is rejected at load instead.
- **Exactly one profile declares `authority: merge`.** Its result is the only
  one that can establish a gate verdict. Every other profile is `advisory`
  (the default): the dev/fix loop is told to run it and told that a pass there
  is not evidence the story is done.
- **Selection is fixed and deterministic.** VALIDATE runs the merge-authority
  profile. The dev/fix loop prefers `targeted`, then `fast`; with neither
  declared it widens to the merge-authority profile. Unknown or empty inputs
  always cause *more* validation to run, never less.
- **Scoping context is supplied, not interpreted.** `{test_target}` (per-story,
  falling back to `default_test_target`) and `{slug}` are substituted into your
  command. What a scoped run means is your command's decision — TheForge infers
  no test-framework syntax, source-to-test mapping, or package layout.
- **A story `gate:` override is not a declared profile.** With profiles declared
  it still runs, but its result is recorded as advisory. A *passing* override
  then widens: the declared merge-authority profile runs too, in the same
  worktree, and its result is the verdict — so no story reaches review or
  landing on a result that carries no merge authority. A failing override
  already blocks, so it does not pay for the complete profile. On the legacy
  (no-profiles) path an override behaves exactly as before.

Every validation run is recorded with its profile, authority, resolved command,
result, and commit — in the audit record (`iterations.validation_runs`), in the
resume sidecar, and in the reviewer prompt — so the standing behind a verdict is
readable afterwards rather than inferred from a command string.

**Omit the block entirely and nothing changes:** `gate_command` remains the
complete, merge-authority run and `test_command` the advisory one.

### Dev verification commands (`validation.dev_verification_commands`)

A capability preset widens the sandbox; it cannot *remove* it. Some toolchains
are structurally incompatible with being sandboxed at all — SwiftPM compiles
package manifests by invoking `sandbox-exec` itself, and macOS refuses to apply
a sandbox inside a sandbox, so the failure is in the isolation mechanism rather
than in its allow-list. No preset fixes that. The dev agent could edit, submit,
and learn the outcome only from the coordinator's gate — which is how one
adopter story burned six iterations and $46 on six identical compile failures.

Per [ADR-0007](../adr/0007-dev-phase-verification-capability.md): **the project
declares whole named commands, and the agent's granted capability is the
request, never the execution.**

```yaml
validation:
  dev_verification_commands:
    verify-watch: "xcodebuild -scheme Watch -destination 'platform=watchOS Simulator' test"
    verify-app:
      command: "xcodebuild -scheme App test"
      timeout: 1200              # seconds; default 600
      output_tail_chars: 8000    # returned to the agent; default 4000
  dev_verification_max_requests: 4
```

The dev prompt then lists these names and the request protocol. The agent writes
`{"command": "verify-watch"}` into a per-iteration request directory under the
worktree and polls for the response artifact; the coordinator runs the command
in the worktree, outside the sandbox — the same path `gate_command` already
uses — and writes back the exit code, timeout flag, output tail, and a trace
path to the full output.

What makes this bounded:

- **The declared unit is a whole command, not a binary.** The agent supplies
  only a *name*. It never composes argv, so it cannot turn a trusted toolchain
  binary into an arbitrary invocation over build inputs it authored.
- **Unknown names are refused, not executed**, and refusals reach the agent as
  an explicit `accepted: false` with a reason rather than as a broken toolchain.
- **The budget is fail-closed and per-iteration.** Every request counts, accepted
  or refused, so a loop of malformed requests cannot buy unbounded execution.
  It does not reset when a transient transport failure re-attempts the agent.
- **No command outlives its declared `timeout`.** If the agent returns while a
  declared command is still running, the command keeps the rest of its own
  `timeout` to finish and is killed past that — an unconfined build still
  writing to the worktree would otherwise race the coordinator's own
  authoritative gate. `timeout` is the budget the command actually gets, at the
  agent's turn or after it. The kill is recorded as `cancelled: true`, which
  reads differently from a real failure.
- **Malformed declarations fail at config load** — an empty name, a
  path-traversing name, a missing command, an unknown field, or a non-positive
  limit is a config error, not a runtime surprise on something already running
  unconfined.
- **Every invocation is audited.** Each request is recorded per dev iteration
  (`iterations.dev_loop[].verification_requests`) and as a run-level roll-up
  (`workspace.dev_verification_requests`) with its name, accepted/refused status,
  exit code, timeout flag, and trace path.

**Declaring nothing keeps today's behavior exactly**: no request channel is
created, and the dev prompt does not mention the capability. This does not
replace the gate — the coordinator still runs the authoritative gate after the
agent completes.

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
    file: "src/foo.py"        # null allowed for architectural P1s
    line: 42                  # null allowed for file-scope findings
    observed: "What the code actually does"
    expected: "What it should do"
    evidence: "Pointer proving the observation (diff hunk, test, trace)"
story_compliance:
  matches_spec: true | false
  mismatches: []
test_coverage:
  adequate: true | false
  gaps: []
ac_verification:              # one entry per acceptance criterion
  - criterion: "The criterion text (or 'Symptom resolution' for bugs)"
    status: VERIFIED | PARTIAL | NOT_VERIFIED
    evidence: "Diff hunks + test pointers for VERIFIED, reason otherwise"
criteria_enumerable: true     # optional; false requires criteria_enumerable_rationale
```

Every finding requires non-empty `observed`, `expected`, and `evidence`
strings. `file` may be null for architectural findings; `line` may be null for
file-scope findings (a P1 with `file` set must have a non-empty path).

**Contract enforcement (`src/theforge/schemas.py`):** violations are tagged by
stage (`YAML_SYNTAX`, `STRUCTURE`, `SCHEMA_VALIDATION`,
`CONTRACT_CROSS_VALIDATION`) and trigger a corrective retry prompt to the same
reviewer, up to `retry.max_review_parse_retries` (default 2). Nothing is
silently rewritten.

- `APPROVE` with any P1 → `CONTRACT_CROSS_VALIDATION` error → parse retry
- `REQUEST_CHANGES` with no P1 → error (must have a P1 to justify it)
- `APPROVE` with empty `ac_verification` → error, unless the reviewer declares
  `criteria_enumerable: false` with a non-empty
  `criteria_enumerable_rationale`
- `APPROVE` with any `PARTIAL`/`NOT_VERIFIED` entry → error
- Output still unparseable after retries → that reviewer's verdict is recorded
  as `REQUEST_CHANGES` with `matches_spec: false`
- `spec_compliance` is accepted as a backward-compatible alias, but new
  reviewers should emit `story_compliance`.

After parsing, findings pass through mechanical disposition classification
(`coordinator/review_phase.py`): a P1 asserting a test/build failure that a
PASS gate mechanically disproves is downgraded (`gate_contradicted`) unless
that reviewer also reported `matches_spec: false`.

Before any of that, a P1 must be **diff-grounded** to be eligible to block: the
file it cites has to appear in the story's own merge-base-to-HEAD diff. A P1
naming a file this story never touched — or citing no resolvable file, or raised
when the diff could not be computed — is recorded as `diff_ungrounded`. It stays
in `finding_registry` and appears under `non_blocking_p1s` in the audit record,
but it blocks nothing, is not promoted by `matches_spec: false` or by
`allow_net_new_bypass: false`, and is not handed back to the dev agent as work to
fix. This is what keeps a sibling story's acceptance criteria from failing an
unrelated story batched into the same sprint. The same check runs on the
review-only path that sprint batch members go through, where a `REQUEST_CHANGES`
whose every P1 is `diff_ungrounded` completes the story instead of escalating —
recorded as `REQUEST_CHANGES→diff_ungrounded_pass`, not as a plain approval.

`diff_ungrounded` describes **one cycle's diff, not the finding**. The verdict is
re-decided from scratch every review cycle, in both directions: a P1 suppressed
in cycle 1 blocks again in cycle 2 if the dev has since touched the file it
cites, because at that point it is squarely about this change. Nothing carries
the suppression forward — the classifier gives a recurring finding its ordinary
disposition and grounding is the only thing that writes `diff_ungrounded`.

Inside a **cost-aware batch group** the branch carries several independent
stories, so the branch diff is the group's change and not any one member's.
There the file set is narrowed to the commits the shared dev handoff attributes
to that member (the `slug` key each `commits` entry must carry). Being a batch
member is what selects this treatment, not whether the handoff arrived:
attribution that is missing, absent entirely, or unusable yields an *unknown*
file set, which grounds nothing — never a fallback to the group's combined diff,
which is what would let one member's findings decide another's outcome. One
exception keeps this from excusing unfinished work: a member whose own file set
is known and **empty** has no change to judge, so its review still blocks.

The handoff is agent output, so its `sha` values are untrusted input. Each is
validated as a bare hex commit id (7–40 hex chars) before any git call sees it,
and the attribution path runs git through argv rather than a shell. A `sha` that
is a revision expression (`HEAD~2`), a ref name, or carries shell metacharacters
is refused as data — it invalidates that member's attribution rather than being
sanitised into a command.

The audit record's `review_diff_grounding` names the file set, where it came
from (`branch_diff` or `batch_commit_attribution`), whether it could be
established, and which findings failed to ground, so a suppression can be
re-derived rather than taken on trust. Plan review is a separate
contract with its own corroboration rule — single-reviewer, first-occurrence
plan P1s are downgraded to advisory `P1-impl` (`src/theforge/review.py`).

---

## See also

- [Authoring Guide](authoring.md) — how to write a good issue or story by use case (feature, bug, refactor, rollup, docs/chore)
- [Getting Started](getting-started.md) — full setup walkthrough including config examples
- [CLI Reference](cli-reference.md) — all commands and flags
- [Provider Setup Guide](choose-your-provider-setup.md) — forge.yaml profiles for different scenarios
- [Troubleshooting](troubleshooting.md) — common errors and fixes
