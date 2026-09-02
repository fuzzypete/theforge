# CLI Reference

All commands available through the `forge` CLI.

---

## `forge init`

Generate a starter `forge.yaml` and story template in the current directory.

```bash
forge init
```

**Use this when:** Starting a new project with TheForge for the first time.
**Avoid this when:** You already have a `forge.yaml` and want to keep it.

**Flags:**

| Flag | Description |
|------|-------------|
| `--shared-memory` | Track project memory (audit runs + knowledge summaries) in git (default) |
| `--local-memory` | Keep project memory local — omit the project-memory `.gitignore` re-includes |

**Creates:**
- `forge.yaml` — starter config with Claude dev + review defaults
- `stories/TEMPLATE.md` — annotated story template
- `.gitignore` entry for `.forge/.env`

---

## `forge run`

Execute the full pipeline for a single story.

```bash
forge run <story-file> [flags]
```

**Use this when:** You have one story ready and want end-to-end execution.
**Avoid this when:** Running many stories — use `forge sprint`.

**Flags:**

| Flag | Description |
|------|-------------|
| `--verbose`, `-v` | Show tool activity, heartbeats, and raw agent output |
| `--auto-merge` | Merge feature branch into the base branch after APPROVE |
| `--interactive` | Pause at APPROVE for human confirmation |
| `--resume` | Triage an existing worktree and resume from the best phase |
| `--plan <path>` | Inject a pre-written plan and skip PLAN |
| `--slug <name>` | Override the story slug |
| `--config <path>` | Path to `forge.yaml` |
| `--dry-run` | Print prompts/config without invoking agents |
| `--no-notify` | Suppress notifications |
| `--until <phase>` | Stop after a specific phase and preserve the worktree |
| `--from <phase>` | Resume from a specific phase in an existing worktree |
| `--reviewers <N>` | Limit the review pool to the first N reviewers |
| `--max-cycles <N>` | Cap review→dev cycles for this run |
| `--dev-model <provider/model@base_url>` | Override the dev model for one run |
| `--plan-model <provider/model>` | Override the plan model for one run |
| `--base-branch <branch>` | Override the target base branch for this run |
| `--fg` | Run in the foreground instead of detaching |
| `--no-pull` | Skip `git pull --ff-only` before fresh worktree creation |

**Examples:**

```bash
forge run stories/add-auth.md --verbose
forge run stories/add-auth.md --resume --verbose
forge run stories/add-auth.md --plan docs/plans/auth-plan.md
forge run stories/add-auth.md --until plan --fg --verbose
forge run stories/add-auth.md --from review --fg
```

### Flag guidance

**`--resume`**
Use this when a run was interrupted and you want automatic state detection.
`--resume` continues from the pipeline state an earlier attempt recorded, which
includes any escalate-gate decision — see
[Choosing a re-entry path](#choosing-a-re-entry-path) before resuming a story
whose review loop was still owed a cycle.

**`--until` / `--from`**
Use these for partial workflows, inspection checkpoints, or explicit re-entry
into an existing worktree. Avoid combining them with `--resume`.

**`--fg`**
Use this when you want foreground execution. Detached execution is the default.

---

## `forge review`

Run only the review phase on an existing worktree.

```bash
forge review <story-file> [flags]
forge review --issue <N> [flags]
```

**Use this when:** You implemented or fixed something in a worktree and want to
run review without re-running PLAN/DEV. This is also the path that *runs* an
outstanding review cycle on a story that stopped mid-pipeline — `forge sprint
--resume` may skip it. See [Choosing a re-entry path](#choosing-a-re-entry-path).

Name the **story**, not the worktree. A story sourced from a GitHub issue is
never written to disk, so `--issue N` is the only way to review one — it reads
the issue exactly as the sprint path does, and the slug it derives (`issue-N`)
is what points at the existing worktree. Pass a story file *or* `--issue`, not
both.

**Flags:**

| Flag | Description |
|------|-------------|
| `--issue <N>` | Review a GitHub-issue-backed story (instead of a story file) |
| `--worktree <path>` | Explicit worktree path |
| `--auto-merge` | Merge after APPROVE |
| `--verbose`, `-v` | Show reviewer activity |
| `--slug <name>` | Override slug |
| `--config <path>` | Path to `forge.yaml` |
| `--no-notify` | Suppress notifications |

---

## `forge sprint`

Run multiple stories from a sprint manifest or directly from a GitHub milestone or label.

```bash
forge sprint [manifest.yaml] [flags]
forge sprint --milestone "v0.4.0" --budget 50 [flags]
forge sprint --label "sprint-1" --budget 20 [flags]
forge sprint --issues 123,124 --budget 20 [flags]
```

**Use this when:** You want batch execution with shared budget and story ordering. The
manifest argument is optional when using `--milestone` or `--label`.

**Flags:**

| Flag | Description |
|------|-------------|
| `--verbose`, `-v` | Show activity for all stories |
| `--auto-merge` | Auto-merge each approved story |
| `--interactive` | Pause at each APPROVE |
| `--resume` | Auto-triage each story and resume from the correct phase |
| `--milestone <name>` | Run all open issues in a GitHub milestone (requires `--budget`) |
| `--label <name>` | Run all open issues with a GitHub label (requires `--budget`) |
| `--issues <N,M,...>` | Run specific issues by number without a label or manifest |
| `--budget <usd>` | Budget ceiling in USD — required when using `--milestone`, `--label`, or `--issues` |
| `--name <name>` | Override the sprint name (default: milestone or label value) |
| `--parallel <N>` | Run up to N stories concurrently (default: 1) |
| `--dry-run` | Print the resolved issue list without executing |
| `--base-branch <branch>` | Override the target base branch for this run |
| `--config <path>` | Path to `forge.yaml` |
| `--no-notify` | Suppress notifications |
| `--detach` | Queue the sprint on a running daemon and return immediately |
| `--fg` | Run in the foreground instead of detaching |
| `--no-pull` | Skip `git pull --ff-only` before fresh worktree creation |
| `--force` | Bypass the sprint-entry shape gate and run every selected issue |

`--detach` is manifest-only. Query mode (`--milestone`, `--label`, or `--issues`)
must run in the current process, usually with `--fg` when you want foreground logs.

**Sprint manifest format:**

```yaml
name: "Sprint Name"
budget_usd: 50
auto_merge: true
stories:
  - stories/story-one.md
  - stories/story-two.md
  - {issue: 123, slug: add-schema}
  - {issue: 124, slug: use-schema, depends_on: [add-schema]}
```

`depends_on` lists the slugs of sibling stories that must complete first; the
scheduler orders stories along these edges (a story cannot depend on itself).
Collision-derived edges — from preflight predicting overlapping `likely_files`
— are added automatically on top of explicit `depends_on`.

> **Note:** `specs:` is a deprecated alias for `stories:` and still works.

---

## `forge ideate`

Run multi-model deliberation to generate a story from a brief.

```bash
forge ideate <brief-text-or-file> [flags]
```

**Use this when:** You have a fuzzy problem statement and want a structured story.

**Flags:**

| Flag | Description |
|------|-------------|
| `--output <path>` | Write generated story to file (default: `stories/<slug>.md`) |
| `--rounds <N>` | Deliberation rounds, 1-3 |
| `--config <path>` | Path to `forge.yaml` |
| `--dry-run` | Print the synthesized story without writing a file |
| `--verbose`, `-v` | Show deliberation activity |

**Examples:**

```bash
forge ideate "Add rate limiting to the API" --output stories/rate-limiting.md
forge ideate briefs/rate-limiting.txt --output stories/rate-limiting.md
```

---

## `forge check-providers`

Smoke-test API-mode profiles in `forge.yaml`.

```bash
forge check-providers [flags]
```

**Use this when:** Verifying hosted-provider auth and connectivity.

**Flags:**

| Flag | Description |
|------|-------------|
| `--profile <name>` | Test only one API profile |
| `--declared-only` | Exercise only each probe's declared transport |
| `--config <path>` | Path to `forge.yaml` |

**What it records:** every probe that actually validates a capability (structured
output) writes the outcome to `.forge/model_capabilities.yaml`, keyed by
provider/model/transport and stamped with when it was established. Routing reads
that record at dispatch and declines to seat a model whose required capability is
currently demonstrated *absent*, reporting
`capability_demonstrated_absent` in the `routing_decision` block. An identity with
no record stays eligible — never-established is not absence — and a record whose
probe subject has changed since (a repointed `base_url`, a different CLI binary)
is treated as stale rather than current. Re-run this command after changing a
model's endpoint to refresh the record.

If the record rules out *every* candidate for a role, routing refuses the run
rather than seating a model that has demonstrated it cannot do the job — the
error names the role, the capability, and when each absence was established.
Re-run `forge check-providers` if the record is out of date, or configure a model
that can produce the capability. An explicitly pinned role is exempt: the
operator's choice stands and is flagged in that role's routing rationale.

---

## `forge check-config`

Show the effective config, auth readiness, and warnings.

```bash
forge check-config [forge.yaml]
```

**Use this when:** After editing config, before a release, or when debugging model wiring.
This is the quickest way to inspect the role table derived from `models:`.

---

## `forge explain`

Render the operator-facing assignment summary for one recorded run.

```bash
forge explain --story <issue-or-slug>
forge explain --run <run-id>
forge explain --file .forge/audits/runs/<run-id>.json
```

**Use this when:** You need to answer why a model or reviewer was selected,
avoided, deprioritized, or escalated.
**Avoid this when:** You need live process state; use `forge status` for liveness
and the raw per-run audit JSON for full forensics.

**Flags:**

| Flag | Description |
|------|-------------|
| `--story <id>` | Explain the latest recorded run for a GitHub issue number (`270`, `#270`) or slug (`issue-270`) |
| `--run <run-id>` | Explain one exact run ID from the audit substrate |
| `--file <path>` | Render directly from a per-run audit JSON file |
| `--config <path>` | Path to `forge.yaml` when resolving substrate-backed lookups |

`forge explain` is a **read-only view over the recorded `routing_decision`
block**. It does not invoke agents, rebuild profiles, or recompute routing from
live state. The per-run audit record is the contract; this command is one
presentation of it.

The output includes:

- Selected model(s) per role and the recorded rationale
- Candidate pools with canonical exclusion reasons
- Consulted signals, including raw vs. recency-weighted values and sample-floor status
- Adaptive mechanism outcomes, distinguishing not checked / checked and did not fire / fired
- Exploration mode and score-policy details

If a run predates the `routing_decision` contract, the command says so
explicitly instead of fabricating an explanation.

---

## `forge eval-preflight`

Evaluate candidate preflight models against a golden story set.

```bash
forge eval-preflight [flags]
```

**Use this when:** Comparing preflight models without running a full sprint.

**Flags:**

| Flag | Description |
|------|-------------|
| `--golden-set <path>` | Path to `golden_stories.yaml` |
| `--models <A,B,...>` | Comma-separated model identifiers to evaluate |
| `--working-dir <path>` | Working directory for agent invocations |
| `--output-format <text|json>` | Report format |
| `--config <path>` | Path to `forge.yaml` |

---

## `forge telemetry`

Show historical per-phase cost and duration from `.forge/audits/history.jsonl`.

```bash
forge telemetry [flags]
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--since <YYYY-MM-DD>` | Only include runs on or after this date |
| `--phase <phase>` | Show a single phase (`preflight`, `plan`, `plan_review`, `dev`, `validate`, `review`) |
| `--config <path>` | Path to `forge.yaml` |

---

## `forge status`

Show active detached runs and pending decisions.

```bash
forge status [run-id] [flags]
```

For an active sprint run, `forge status` includes the per-story sprint status
view. The old standalone `forge sprint-status` command is no longer exposed by
the top-level parser.

**Flags:**

| Flag | Description |
|------|-------------|
| `run-id` (positional) | Show status for one run (default: active or most recent run) |
| `--recent` | Show recent runs in compact list form |
| `--last` | Show the most recent completed or failed run |
| `--watch [SECONDS]` | Live-update mode: re-render every SECONDS (default 2); falls back to a single snapshot when stdout is not a TTY |
| `--no-color` | Disable ANSI color in watch mode (`NO_COLOR` env var also honored) |
| `--operator-actions` | List open operator-action issues with readiness derived from `depends_on` |
| `--ready` | List open `ready`-labeled issues, checked against the sprint shape gate |
| `--milestone <name>` | Scope `--ready` to one GitHub milestone |

### Run dispositions

The bracketed label in the sprint header is derived from how the run actually
ended — never from the absence of a record:

| Label | Meaning |
|---|---|
| `live` | Owning process is alive (PID file present). |
| `completed` | The sprint returned normally. |
| `stopped` | Terminated deliberately (`forge stop`, SIGTERM). |
| `failed` | Terminated on an unhandled exception; the terminating cause is printed on the `cause:` line below the header. |
| `orphaned` | Marked by `forge runs-clean` — no process, no terminal marker. |
| `crashed` | State file left behind with no terminal marker at all. |

When the owning process is gone, stories whose last recorded phase said
`running` are reported as `interrupted` (their last known phase is retained in
the PHASE column as history). Interrupted work is not progressing — resume the
sprint rather than waiting on it.

### Outstanding phases and re-entry

A story that stopped mid-pipeline can still owe a phase. When it does, its row
carries two extra lines derived from the coordinator's persisted resume record —
no run is started, so this costs nothing:

```
✗ issue-2239   failed   REVIEW   ...   review cycle 1 REQUEST_CHANGES
    outstanding: REVIEW cycle 2 not run
    re-entry: forge review runs REVIEW cycle 2; forge sprint --resume recovers
              escalation decision land_core/defer_edges and skips REVIEW
```

`outstanding:` names what has *not* run — this is what distinguishes a story
with an unrun review cycle from one whose review completed with APPROVE (which
shows neither line). `re-entry:` appears only where the two paths disagree; see
[Choosing a re-entry path](#choosing-a-re-entry-path). The same two lines are
printed under a pending decision whose story is in that state, so the
pending-decision surface cannot hide an unrun review cycle.

A **completed** sprint renders the postmortem digest instead of the table, and
carries the same facts as its own section — printed whether or not an RCA
artifact exists, because what a story still owes is not a failure
classification:

```
OUTSTANDING (1)
  ✗ #2239  REVIEW cycle 2 not run
       re-entry: forge review runs REVIEW cycle 2; forge sprint --resume recovers
                 escalation decision land_core/defer_edges and skips REVIEW
```

### Operator-action queue

```bash
forge status --operator-actions
```

Lists open `operator-action` issues with a readiness indicator so the operator
can see, at a glance, which operator-owned actions are next:

- **ready** — every issue the operator-action `depends_on` is closed (or it
  declares no dependencies).
- **blocked** — at least one `depends_on` issue is still open. The output names
  the pending dependencies and counts them.

Readiness is recomputed from GitHub on each invocation, so re-running the
command reflects current dependency state without a manual refresh.

```
$ forge status --operator-actions
Operator-action queue (2 issues):
  ready    #1471  Validate v0.11 substrate (deps: #1326 ok, #1437 ok)
  blocked  #1480  Cut v0.11.0rc1 (deps: #1450 open, #1469 open - 2 pending)
```

### Ready-for-next-sprint set

```bash
forge status --ready                       # all open `ready`-labeled issues
forge status --ready --milestone v0.10.0   # scoped to one milestone
```

Lists open issues carrying the `ready` label — the set eligible for the *next*
sprint via normal selection. This surfaces the mid-sprint
[groom-and-ready convention](authoring.md#mid-sprint-workflow) without inventing
a new command: there is **no `forge queue`**, and this listing adds no ordering
or priority semantics. It is simply the current eligible set, recomputed from
GitHub on each invocation.

`ready` is a human-applied label and nothing enforces that it is applied only
after `capture → shape → diagnose → groom`. So each entry is also run through
the same shape gate that guards sprint entry: an issue the gate would refuse is
marked `BLOCKED:<verdict>` instead of `ready`, and its refusal is spelled out
below the listing. Selecting only the `ready`-marked entries cannot produce a
story the sprint refuses.

```
$ forge status --ready --milestone v0.13.0
Ready for next sprint in v0.13.0 (2 issues, 1 blocked by shape gate):
  #1487  bug  ready                    status --watch blank during preflight
  #1512  bug  BLOCKED:needs_diagnosis  cut-rc.sh shim wrapper regression

1 issue carries the `ready` label but would be refused at sprint entry:
  #1512  needs_diagnosis: Bug has no Diagnosis section — not fix-ready. …
Run `forge shape <n>` for the full verdict, then `forge groom <n>` / `forge diagnose <n>` before sprint selection.
```

---

## `forge logs`

Tail the log file for a running detached run.

```bash
forge logs <run-id>
```

During a sprint, drill into a single story instead of the interleaved
sprint-level log:

```bash
forge logs <run-id> --story <slug>   # tail one story's run log
forge logs <run-id> --story          # list the sprint's stories + current phase
```

`--story` with no argument prints each story slug alongside its current phase,
so you can pick a slug without hunting for the nested log path yourself.

---

## `forge stop`

Send `SIGTERM` to a running detached run.

```bash
forge stop <run-id> [--no-wait] [--timeout N]
```

By default, `forge stop` waits up to 60 seconds for process exit.

---

## `forge decide`

Record a decision for a pending HITL checkpoint.

```bash
forge decide <run-id> <action>
```

Common actions are `approve`, `reject`, `continue`, `retry`, `skip`, and `abort`.

---

## `forge runs-clean`

Mark orphaned runs with no terminal marker so `forge status` shows accurate state.

```bash
forge runs-clean
```

---

## `forge daemon`

Manage the legacy persistent daemon runner.

```bash
forge daemon <start|stop|status|install|uninstall>
```

`forge daemon` is deprecated now that `forge run` and `forge sprint` auto-detach
by default, but it remains available for daemon-specific workflows.

---

## `forge secrets-init`

Create a `.forge/.env` skeleton for API keys.

```bash
forge secrets-init
```

**Use this when:** Setting up API-mode providers such as OpenAI, Google, Anthropic, or DeepSeek.

---

## `forge init-hooks`

Scaffold `.forge/hooks/post_run.sh` and hook documentation.

```bash
forge init-hooks
```

---

## `forge author`

Interactively collect the required parts of a typed issue body before it is
filed or promoted out of `todo:draft`. The command renders through the shared
typed issue specification and validates with the same shape gate used
everywhere else before any GitHub mutation is attempted.

```bash
forge author --type enhancement
forge author --from-draft drafts/issue-2408.md --output drafts/issue-2408.md
forge author --from-issue 2408 --create
```

**Use this when:** You want the path of least resistance to be a first-pass
body that already satisfies the intake rules, or you need to resume a saved
local draft / `todo:draft` issue without rediscovering the constraints by
refusal.

If a required part is still missing, `forge author` reports it at the prompt
where it matters, keeps or adds `todo:draft`, and writes an honestly
incomplete draft instead of submitting something that reads ready. Acceptance
criteria are collected as reviewer-checkable outcomes; implementation-plan
details such as file paths, call sequences, and design notes belong outside
the issue body. See [Authoring Issues and Stories](authoring.md) for the
per-use-case guidance and examples.

---

## `forge shape`

Classify a rough draft (issue, file, or stdin) into a typed work object —
`bug`, `enhancement`, `epic`, `operator-action`, `documentation`,
`adr-candidate`, or `duplicate/stale`. Refusal-capable: low-confidence inputs
are kept as `todo:draft` with structured ambiguity questions rather than
force-classified.

```bash
forge shape <issue>          # classify a GitHub issue
forge shape --from-brief FILE
forge shape --from-stdin
forge shape <issue> --apply  # commit the label + body edits via gh
forge shape <issue> --next   # print only the recommended next command
```

**Use this when:** You captured a rough thought (`forge todo`, a brain-dump
file, or a freshly-filed issue) and need to know what kind of work object it
should become before grooming or diagnosing it.

**The command never auto-invokes a producer.** `--next` prints a hint
(`forge diagnose --issue 123`, `forge groom 123`, etc.); it does not run it.
`--next` output is human-readable in v0.11; no stable machine-readable
contract is promised yet. Its exit code carries the same readiness signal as
the default path — 0 only when the recommendation is terminal.

**ADR candidates** receive a proposed slug and title — the ADR file is not
written. **Epic** classifications may propose child stories in prose; no
child issues are created.

**The body restructure is strictly additive.** It only appends what the shape
gate reports missing — an absent `## Observed` / `## Expected` section, or the
specific `## Diagnosis` components the gate names — inserting missing diagnosis
components into the existing `## Diagnosis` section. Existing headings are never
re-levelled, rewritten, or demoted into quoted prose. A bug body that already
passes the shape gate produces no diff.

**The proposal is a diff against observed state, and the exit code is the
readiness signal.** Labels the issue already carries are never proposed, and
`forge shape <issue>` exits 0 exactly when the issue already satisfies the
shape gate — nothing left to add, remove, or restructure, and the gate's
verdict on the current body is `runnable`. In that case the recommendation is
terminal:

```
Next: none — #2050 already satisfies the shape gate (verdict: runnable); no further action is needed.
```

Any other state exits 1 and names the stage that still owes work (`forge
diagnose`, `forge groom`, splitting an epic, closing a duplicate). This holds
for `--next` too. `--apply` is the exception: it reports whether the mutation
succeeded, not whether the issue is ready, and on a gate-passing issue it makes
no `gh` call at all.

Every invocation writes a row into the audit substrate's `shape_events`
table (issue number, input source, classification, confidence, ambiguity
question count, whether `--apply` mutated the issue, and the shape-gate
verdict observed).

`forge shape` is the first step of the mid-sprint capture flow — see
[Mid-sprint workflow](authoring.md#mid-sprint-workflow) for the full
`capture → shape → diagnose → groom → ready` sequence.

---

## `forge diagnose`

Discover the root cause of a symptom-only bug so it can move from
investigation-ready to implementation-ready. Bugs without a confirmed cause
cannot be labeled `ready` (see `forge groom` below).

```bash
forge diagnose --issue <issue>          # investigate one issue
forge diagnose --issue <issue> --interactive
forge diagnose --issue 101,102 --parallel 2
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--issue <N>` | Issue number(s) to diagnose — comma-separated or repeat the flag (required) |
| `--interactive` | Operator-in-the-loop: confirm before landing the artifact |
| `--autonomous` | Land the artifact without operator confirmation (overrides config default) |
| `--output <dest>` | Where to land the diagnosis artifact (overrides `forge.yaml` `diagnose.output_destination`) |
| `--parallel <N>` | Maximum concurrent issue diagnoses (default: serial) |
| `--timeout <seconds>` | Per-invocation timeout override (overrides `forge.yaml` `diagnose.timeout_seconds`) |
| `--dry-run` | Run the investigation but do not land the artifact |
| `--verbose`, `-v` | Enable verbose logging |
| `--config <path>` | Path to `forge.yaml` |

Whether landing requires confirmation defaults from config; `--interactive`
and `--autonomous` override it per invocation.

`forge diagnose` is the third step of the mid-sprint capture flow — see
[Mid-sprint workflow](authoring.md#mid-sprint-workflow) for the full
`capture → shape → diagnose → groom → ready` sequence.

**Diagnosing an issue filed by `forge report`.** An issue filed from the project
where the behavior was observed carries that run's evidence with it — a manifest
in the body, the artifacts as comments. `forge diagnose` reads that payload and
answers questions about the observed run *from it*:

```
$ forge diagnose --verbose --issue 2571
  evidence   : attached bundle from fuzzypete/hdp (run f5aa21cf2d8d, forge v0.14.2)
  reading    : run log, story audit, sprint state, reviewer outputs, story body
  unreadable : intake candidate artifacts (absent from bundle)
```

On this path the checkout the diagnosis executes in is treated as a *different
runtime*, because it is:

- The issue-body reference pre-load is skipped — it resolves references against
  this checkout, which is the wrong project.
- No baseline SHA is stamped, agent-reported paths are not hashed against local
  git, and the premise check is not run: a path this repository never had (or
  removed for unrelated reasons) must not report a live cross-project defect as
  already resolved.
- Anything the bundle does not carry is reported as unreadable and named in the
  diagnosis, never filled in from local configuration or source. That
  substitution is the failure this path exists to prevent: reading
  `injection: false` from this checkout's default inverts the answer for a run
  that had it on.

Attached artifacts are rendered as untrusted data inside explicit boundaries.
Content that came out of another project's agents is evidence about a run — text
inside it that reads as an instruction, a permission grant, or a conclusion is
reported as something the artifact says, not acted on.

The audit's `attached_evidence` block records the source project, run id, forge
version, the artifact labels actually read, the unreadable ones, and that the
local baseline and premise check were skipped. An issue with no attached
evidence diagnoses exactly as before, with `attached_evidence.source` empty.

**Recovering a run that failed at PARSE.** A completed investigation is the
expensive part of a diagnose run, so a syntax error in the YAML it emits does not
discard it:

- The agent's **complete** output is written to
  `.forge/audits/diagnose-issue-<N>-<run-id>.raw.txt` before the first parse
  attempt, on both the success and the failure path. The audit YAML's
  `agent.raw_output_tail` is a bounded preview; `agent.raw_output_path`,
  `agent.raw_output_paths`, `agent.raw_output_chars`, and
  `agent.raw_output_sha256` point at the full copy on disk.
- Persistence is guaranteed rather than best-effort. If the audit sidecar cannot
  be written, the output goes to `.forge/logs/diagnose-<N>/run-<run-id>.raw.txt`;
  if that also fails, the audit record carries the complete output inline as
  `agent.raw_output`, and `agent.raw_output_error` names every location that
  refused it. Read `agent.raw_output_path` first — an empty value means the
  content is inline. There is no path on which a run consumes a paid-for
  investigation and leaves only the truncated tail.
- Unparseable output triggers up to `retry.max_diagnose_parse_retries`
  (default 2) **reformat-only** retries: the agent gets its own output back plus
  the parser error and is asked to re-serialize the same diagnosis — it does not
  re-investigate. Each attempt writes its own
  `…-<run-id>.raw.retry<N>.txt` sidecar and one `agent.parse_retries[]` audit
  entry with the parse error, cost, duration, and outcome.
- If the retries are exhausted the run still fails, but the diagnosis is
  readable from the sidecar and can be applied by hand with `gh issue edit`
  rather than paying for a second investigation.

---

## `forge groom`

Restructure a typed issue body to satisfy the shape-gate rules for its type
(bug, enhancement/task, docs). Operates on one issue per invocation; does
not invoke `forge diagnose` or `forge shape` — operator runs those
explicitly in the `capture → shape → diagnose → groom → ready → sprint`
lifecycle.

```bash
forge groom <issue>            # show proposed body diff (exit 2 if changes needed)
forge groom <issue> --apply    # commit the restructure via `gh issue edit`
forge groom <issue> --next     # also print a recommended next operator command
```

`--confirm-diagnosis-current` is the operator's assertion that a bug's
diagnosis is still valid against the current base even if the recorded
baseline is stale; the assertion is recorded in the audit substrate so the
decision is auditable rather than silent.

`<issue>` is a GitHub issue number (`1503` or `#1503`) or a local issue
body file path.

**Three-state bug handling.** Bug-typed issues are routed by the diagnosis
state present in the body:

| Diagnosis state         | Behavior                                                                                          |
|-------------------------|---------------------------------------------------------------------------------------------------|
| No diagnosis            | Refused with `"needs diagnosis — run forge diagnose <N> first."` No body edits proposed.          |
| Diagnosis, cause unknown| Body normalization only; output says "investigation-ready, not implementation-ready." No ready.   |
| Diagnosis, confirmed    | Body is restructured; post-groom verdict reported. Operator may then label `ready`.               |

`forge groom` will never propose adding the `ready` label to a bug whose
cause is unknown — this is a hard invariant of the lifecycle.

**Groom restructures; it does not write content.** For a feature/task/docs
issue missing acceptance criteria or an example, groom inserts the section
heading with a `TODO(forge-groom)` placeholder — and the corresponding
shape-gate finding *stays*. Those findings are listed under `UNRESOLVED:` in
the output and recorded as `unsupplied_findings` in the audit event, so the
gap is carried by the issue instead of erased from it. Fill the placeholder
sections in yourself before labeling `ready`; a groomed-but-unfilled issue
will not reach the `runnable` verdict. This is enforced mechanically: any
proposal that resolves `missing_acceptance_criteria` or `missing_example` is
discarded and the body falls back to whitespace normalization.

**`--next` is operator hint, not protocol.** Output is human-readable; the
v0.11 contract does not promise a stable machine-readable shape. A `--json`
extension may follow when auto-routing in v0.12+ needs it.

Every invocation emits a `groom` row to the SQLite audit substrate
(`.forge/audits/index.sqlite`, table `readiness_events`) so refusal counts
and investigation-ready piles are queryable.

`forge groom` is the fourth step of the mid-sprint capture flow — see
[Mid-sprint workflow](authoring.md#mid-sprint-workflow) for the full
`capture → shape → diagnose → groom → ready` sequence.

---

## `forge audit`

Display a human-readable summary of an audit file.

```bash
forge audit <audit-file.yaml>
```

---

## `forge version`

Print the installed version, and in editable installs show branch, commit, and
tag distance.

```bash
forge version
```

---

## `forge index`

Generate `.forge/index/modules.yaml`, the module map preflight and collision
detection read to reason about the repository.

```bash
forge index
```

Two derived-index modes share the command:

```bash
forge index --knowledge    # .forge/knowledge/index.yaml from persisted run summaries
forge index --invariants   # .forge/knowledge/invariants/index.yaml from forge-invariant markers
```

`--invariants` scans the Markdown globs in `knowledge.invariant_sources`, prints
`path:line: reason` diagnostics for malformed annotations on stderr, and writes
provenance metadata only — never invariant prose. See
`docs/plans/1875-invariant-index-spike.md`.

---

## `forge check-story-config`

Reject story-branch `forge.yaml` edits that fall outside the mutable allowlist.
Runs as part of `make gate`; exit non-zero if a disallowed edit is present.

```bash
forge check-story-config
```

---

## `forge todo`

Capture and triage draft (`todo:draft`) GitHub todo issues. With no action it
lists open drafts; pass title text to create one, or a subcommand to triage.

```bash
forge todo                       # list open todo:draft issues
forge todo list                  # same as above, explicit
forge todo "tighten gate scrub"  # create a draft todo
forge todo triage <n>            # triage draft issue #n
forge todo promote <n>           # promote draft issue #n
```

Optional flags record provenance: `--from-sprint`, `--issue`, `--run-id`.

---

## `forge triage`

`forge triage` has three supported modes, and whether a bare invocation writes a
report or rejects depends on whether an operator is present (stdin is a TTY):

- With no `--report` **and an operator present**, it generates the deterministic
  backlog report for every open finding-labeled issue.
- **Without an operator** (cron, a post-sprint pass, any non-interactive
  invocation), it rejects because disposition proposals are shelved by
  ADR-0010. See
  [Headless mode](#headless-mode) below.
- With `--report <path>`, it rejects because consuming backlog reports for
  disposition proposals is shelved by ADR-0010.
- With `--ratify <triage-run-id | pending-id>`, it loads one recorded proposal
  run from the audit substrate, prompts the operator finding-by-finding, and
  applies only the ratified dispositions. Requires a TTY.
- With `--discard <pending-id | triage-run-id>`, it drops a persisted pending
  triage decision without applying anything.

```bash
forge triage
forge triage --output .forge/triage/report.yaml
forge triage --report .forge/backlog-report.json   # rejects; proposals are shelved
forge triage --ratify 4601fd03e0c7
forge triage --discard triage-4601fd03e0c7
```

### Deterministic backlog report mode

Bare `forge triage` inventories the backlog as the union of open issues carrying
`forge-finding`, `review-finding`, or `needs-triage`. For each finding it
reports:

- age (days since the GitHub issue was filed),
- current pool state (`needs-triage`, milestone title, or `unpooled`),
- mechanically gathered evidence only:
  - whether a cited repo path still exists,
  - whether a cited symbol is still present,
  - how many times the cited file changed since the finding was filed.

No tracker writes occur on any path, and no model is invoked. Findings whose
citations partly resolve are reported as `PARTIALLY-VERIFIED`; findings whose
citations cannot be checked at all remain `UNVERIFIED` with the reason. Entries
that could not be checked are never omitted and never upgraded into stale
claims.

Sample output:

```text
FINDING BACKLOG — 15 open (14 hygiene-pool, 0 needs-triage, 1 unpooled)

#1312  bug,p2     Hygiene      age 84d    STALE-EVIDENCE: cited symbol
       _validate_auto_api_fallback_schema absent from current tree
#1124  bug,p2     Hygiene      age 102d   PARTIALLY-VERIFIED: cited path
       src/theforge/cli/triage.py present in current tree
       could not attribute cited filename audit.yaml
#659   bug,p2     unpooled     age 197d   UNVERIFIED: body cites no
       checkable artifact (prose-only or unparseable citation)

structured report: .forge/triage/report-20260823T000000Z.yaml
```

The structured artifact is YAML by default and is what downstream triage stages
consume. The producer adds inventory metadata, but the consumer boundary is the
same `findings` / `evidence` contract already parsed by `theforge.triage_report`.

Artifact shape:

```yaml
generated_at: 2026-08-23T00:00:00Z
current_milestone: v0.12.0        # optional; still the fix_now target
named_milestones: [v0.13.0]       # optional; later fix_later targets
summary:
  total_open: 15
  by_state:
    Hygiene: 14
    needs-triage: 0
    unpooled: 1
  by_verification_status:
    active: 7
    partially_verified: 1
    stale_evidence: 5
    unverified: 2
findings:
  - finding_id: "#1312"           # stable identity for this issue-backed finding
    issue_ref: "#1312"
    issue_number: 1312
    title: "audit count is off by one"
    body: "..."
    labels: [bug, p2, forge-finding]
    display_labels: bug,p2
    opened_at: 2026-05-31T00:00:00Z
    age_days: 84
    pool_state: Hygiene
    verification_status: stale_evidence
    evidence:
      - id: symbol:_validate_auto_api_fallback_schema:absent
        kind: staleness
        summary: cited symbol _validate_auto_api_fallback_schema absent from current tree
        checkable: true
        detail: searched for _validate_auto_api_fallback_schema at HEAD
        observed_status: stale_evidence
```

Evidence entry ids are deterministic and may be range-scoped. File evidence uses
`path:<repo-relative-path>:<suffix>` or
`path-line:<repo-relative-path>:<line[-end]>:<suffix>`, and symbol evidence
uses `symbol:<name>:<suffix>`. The generator emits more than `staleness` rows:
`artifact_presence`, `churn`, and `unverified` appear in the same `evidence`
list and are accepted by the downstream consumer. Common examples include
`path:src/demo.py:churn`, `path-line:src/demo.py:1-3:absent`, and
`path:config.yaml:unverified`.

If the backlog is empty, the command prints an explicit zero-backlog success and
still writes the structured artifact.

### Advisory proposal mode

`forge triage --report <path>` is shelved by ADR-0010. The command rejects this
invocation before it loads the report, dispatches no proposer or reviewer, and
prints the ADR record together with the supported alternative:

```text
[forge] triage: disposition proposals are shelved (ADR-0010).
        A full pass returned 29 needs_verification of 30 findings: the proposer's
        evidence is artifact presence and churn, which cannot establish whether a
        behavioral claim still holds.
        To verify findings instead: forge diagnose --dry-run --issue A,B --parallel 2
        Still supported: report generation (no flag, interactive), --ratify, --discard.
        See ADR-0010.
```

If `--report` names a file that does not exist or does not match the report
contract, the shelving rejection still wins: the proposal mode is unavailable
regardless of artifact validity.

### Ratification and application mode

`forge triage --ratify <triage-run-id>` is the operator boundary. It reads the
recorded proposals and punt reviews for exactly that `triage_run_id`, renders
the reviewed context back to the operator, then prompts per finding for
`accept`, `override`, or `skip`.

Existing pending triage decisions remain visible in `forge status` under
`Pending decisions`, including the exact `forge triage --ratify ...` and
`forge triage --discard ...` commands for that record. `forge decide` does not
accept triage pending ids; it refuses them and points the operator back to
those same triage commands.

Properties worth knowing:

- **Nothing is applied without ratification.** Accept applies exactly the stored
  proposal; override applies the operator's alternative instead. In both cases
  the operator decision, the applied disposition payload, and the cited
  evidence refs are written to `triage_application_records` before any tracker
  mutation is attempted.
- **Punts require a fixed reason code and an evidence comment.** A punt may be
  applied only with one of `verified-stale`, `superseded`,
  `not-reproducible`, `duplicate`, or `out-of-scope-by-policy`. The flow posts
  a closing comment carrying a stable idempotency marker, the reason code, the
  cited evidence refs/quotes, and the operator note, then closes the issue. A
  missing or unrecognized code is refused and the finding stays open.
- **Fix dispositions apply the confirmed target.** `fix_now` and `fix_later`
  use the milestone or `Hygiene` target the operator confirmed at ratification,
  even when it differs from the proposal. `needs_verification` records the
  decision but performs no tracker mutation.
- **Stale findings fail closed.** If the live issue/report state no longer
  matches the stored finding snapshot — closed meanwhile, labels/pool changed,
  or the deterministic evidence changed under new commits — the finding is
  marked `stale` and skipped rather than applied blindly. Proposal runs that
  predate stored snapshots are treated as stale for the same reason.
- **Application is resumable and idempotent.** The per-finding application row
  is updated in place, so rerunning `--ratify` completes any remaining
  `ratified`/`failed` rows without re-prompting or duplicating work already
  visible on the issue. Existing milestone assignments, existing close comments
  carrying the marker, and already-closed punts carrying that same marker are
  treated as already applied. A close that happened without the recorded marker
  still fails closed as stale.

Sample output:

```text
TRIAGE RATIFICATION — run 4601fd03e0c7; 3 finding(s)
Applied: 2, stale: 1, skipped: 0, failed: 0

#1312  APPLIED (accept punt/verified-stale)
       Posted closing comment and closed the issue.

#943  APPLIED (override fix_later -> Hygiene)
       Set milestone to Hygiene.

#659  STALE (accept needs_verification)
       Skipped instead of applying.
       stale: live finding state diverged from the reviewed snapshot
```

### Headless mode

A `forge triage` invocation with no TTY on stdin is shelved on the same terms.
Instead of collecting a report, proposing, reviewing, or writing a pending
decision, it rejects with the same ADR-0010 guidance shown above. This includes
the post-sprint helper when a project has opted into `sprint.post_sprint_triage:
true`.

`--ratify` is unchanged: it still refuses without a TTY because ratification is
an operator action and not part of the shelved proposal paths.

### Post-sprint trigger

Set `sprint.post_sprint_triage: true` in `forge.yaml` to run one headless triage
pass after a sprint reaches its terminal result. It ships **off**.

```yaml
sprint:
  post_sprint_triage: true
```

The pass is best effort: it logs the same ADR-0010 shelving guidance and never
fails, blocks, or changes the outcome of the sprint that triggered it. It does
not dispatch proposers, spend money, or write a pending triage decision.

---

## `forge rca`

Regenerate a sprint's root-cause-analysis artifact (`sprint-rca.yaml`) for a
completed run.

```bash
forge rca <run-id>               # write/refresh the artifact
forge rca <run-id> --check       # reproducibility check; exit 2 if it drifted
```

`--refresh` overwrites an existing artifact; `--check` compares the stored
artifact against a fresh generation without writing.

---

## `forge report`

File a forge bug **into another repository from the project where you observed
it**, carrying that run's evidence with the report. Run it in the consuming
project; the evidence is captured there, so nothing has to be copied between
checkouts and no later reader has to reconstruct which release was installed
where.

```bash
forge report --run f5aa21cf2d8d --to fuzzypete/theforge \
  --description "Sprint resume reported a story merged when no commit landed."
forge report --run f5aa21cf2d8d --to fuzzypete/theforge --description - --dry-run
```

`--run` accepts either domain of run id: a story run
(`.forge/audits/runs/<id>.json`) or a sprint run, whose run-keyed summary names
every `story_run_id` in it — a sprint id reaches every story's record.

The created issue carries the operator's description, a bug-shaped `Diagnosis`
section, and an evidence manifest:

```
forge version : 0.14.2
observed in   : fuzzypete/hdp
run           : f5aa21cf2d8d  (sprint issues-320,324,331)  stories: issue-320
config        : resolved snapshot attached (412 recorded keys, resolved sha256 02edf039db0d, unchanged during run)
artifacts     : run log, run record, per-story audit, sprint state, reviewer outputs, story body
missing       : intake candidate artifacts
publication   : complete — 18 evidence comments attached
```

The `missing:` line is load-bearing. Everything the report asserts about the
run — forge version, runtime identity, resolved configuration — is read out of
the recorded run artifacts; when a part of the record is unavailable it is
named, with its reason, rather than emitted as an empty artifact. A report with
a gap never reads as complete.

The artifacts themselves are attached as comments. The body states its own
publication state: it is created saying `INCOMPLETE`, listing every expected
comment, and is only rewritten to `complete` once every one has landed. If a
comment fails to post, the body is corrected to mark those items `NOT ATTACHED`
and the command exits non-zero.

Before filing, the body is evaluated against **the target repository's own
shape gate**, not this checkout's. The command resolves that repo's default
branch, pins its head commit, downloads `src/theforge/shape_check/` at that
sha, and runs it in an isolated subprocess:

```
shape gate    : diagnosis_cause_unknown (target gate fuzzypete/theforge@1a2b3c4d5e6f (main))
  - [advisory] diagnosis_cause_unknown: no confirmed cause is asserted
```

The verdict names the revision that produced it, so it is the state the target
actually holds — the observing project routinely runs an older release than the
repo it reports into, and a locally computed verdict would name a gate state
that repository does not have. There is no fallback to the local gate: if the
target's gate cannot be resolved, downloaded, executed, or read, the body has
no known gate state and nothing is filed.

Useful flags: `--title`, `--description-file`, `--symptom`, `--cause`,
`--code-path`, `--fix-criterion` (a report filed the moment a defect is seen
defaults to an explicit "not yet identified" cause, which the gate recognises
as investigation-ready), `--label`, `--max-comments`, and `--dry-run`.

---

## `forge batch-report`

Post-sprint batchability analytics for a completed run: which stories would
have qualified for cost-aware batching, what each actually cost per phase,
which stories conflicted / retried / escalated (and were therefore
disqualified), and whether a shared dev pass would have been cheaper. Stories
on either end of a `depends_on` edge are excluded too — a batch is dispatched
as one unit, so it cannot honour an ordering constraint crossing its boundary.

```bash
forge batch-report <run-id>                     # human-readable terminal report
forge batch-report <run-id> --format yaml       # structured payload
forge batch-report <run-id> --format json
```

The batched-cost figure is an **estimate**, labelled as such wherever it
appears: per-story preflight cost is preserved (eligibility is decided from
per-story preflight facts, so preflight does not amortise) and each shared
downstream phase is charged at the highest measured cost across the group's
members. It is a ceiling on savings, not a forecast — the methodology is
printed with every report.

`--max-stories`, `--max-complexity-budget`, and `--max-touched-files` vary the
hypothetical grouping rules for sensitivity analysis. They are deliberately
independent of the project's `sprint.batch` config: batching is off by default,
and a report that inherited the off switch could not measure the opportunity it
exists to measure.

---

## `forge knowledge-report`

Is the knowledge feed-forward loop earning its keep? Compares stories that
actually received prior-run summaries against comparable stories that did not,
on plan regeneration rate, review restated-finding rate, dev/review iteration
counts, and cost per completed story / stories per dollar.

```bash
forge knowledge-report                          # all recorded runs, terminal report
forge knowledge-report --recent-run-count 30    # bound by run count
forge knowledge-report --since 2026-08-01 --until 2026-08-31
forge knowledge-report --format json            # structured payload (also: yaml)
```

Cohorts come from the audit record's context manifests, never from config: a
run is **with_prior_summary** only when an eligible phase (plan / dev / review)
recorded `prior_run_context.enabled: true` *and* included at least one summary.
Enabled with nothing included is a descriptive contrast group, not an
independently assigned control cohort. Disabled or unrecorded is
`unclassified` and never enters a comparison — a run from before the feature
existed is not evidence about the feature.

Comparisons are bucketed by preflight work type, complexity band, and domains,
and only buckets holding both cohorts contribute; "stories with prior knowledge
did better" means nothing if those stories were also smaller.

Cost metrics count all recorded spend in the numerator, including failed or
abandoned runs, while only successful runs with measured cost contribute the
completed-story denominator. Runs with null `cost.total_usd` are excluded from
both sides of the cost metrics and called out explicitly as unmeasured-cost
exclusions, never treated as free runs.

The report distinguishes ordinary **insufficient_data** from a descriptive-only
comparison the current cohort classifier cannot turn into a causal verdict. For
the shipped with/without-prior cohorts, the report explains that
`without_prior_summary` forms only when enabled selection included nothing, so a
causal with-prior vs without-prior verdict is structurally unreachable under
that classifier.

### Prior-run uptake (per run)

Below the cohort report, each run in the window renders its **prior-run uptake**
block — a separate question from the cohort metrics above, and deliberately not
folded into them. It compares the claims the selector rendered into that run's
prompts against the findings that run's review recorded, and reports where the
two correspond:

```text
Prior-run uptake
  claims rendered         9   (7 to dev iter 1, 2 to review iter 1)
  claims eligible         7   (excluded: 2 rendered_to_review_only_not_author)
  review findings         4
    corresponding to an eligible claim   1
      -> claim 3, ref 27bb13e86070:a1b2c3d4e5f6, run 27bb13e86070, dev iteration 1
    not matched to an eligible injected claim   3
    indeterminate   0

  method: rendered-claim-overlap v1 — agreement with labelled set 0.909 (n=11)
  missed-uptake indicator only; contributes to no effectiveness verdict
```

A claim is weighed against a finding only where it **reached the author of the
reviewed work before that finding was recorded**. A claim rendered only into the
reviewer's own prompt is never eligible to explain that reviewer's finding — the
reviewer raising it is the claim working, not the development agent ignoring it.
Preflight renders signals rather than claim prose (ADR-0002 clause 5) and
contributes no claims.

Timing is part of eligibility, not just of matching. A claim rendered after
*every* finding the run recorded could not have been acted on by any of them, so
it is excluded as `rendered_after_every_recorded_finding` rather than counted as
eligible and then reported unmatched — a run holding nothing else has no
correspondence to compute at all. A finding whose own recording time was never
captured does not rule a claim out: unknown order is reported as `indeterminate`,
not as late.

Read the categories exactly as written:

| Reported as | Means |
|---|---|
| `corresponding to an eligible claim` | The finding restates a claim the author had been shown. **Not** proof guidance was ignored — the claim may have been irrelevant, ambiguous, or superseded. |
| `not matched to an eligible injected claim` | No correspondence was found. This is **not** a novelty claim; failing to match establishes only the absence of a match. |
| `indeterminate` | The comparison could not decide — too little finding text, or a matching claim that could not be ordered against the finding in time. |
| `no eligible claims` / `no review findings` | Nothing to compare. Distinct from a run that was compared and matched nothing. The block names which way it failed: no claim reached the author, or every claim that did arrived after the last finding. |
| `uncomparable — run predates claim-exposure capture` | The run never recorded what its agents were shown, so its findings correspond to *unknown*, not to zero. |

The method name and version are recorded with every figure. Agreement is
measured against the stored labelled examples in
`docs/knowledge-uptake-labels.yaml`, which cover correspondences,
non-correspondences and eligibility exclusions; when agreement cannot be
measured — the set is missing, or was labelled against a different method
version — the figures are still shown but marked `UNVALIDATED`.

This is an indicator of missed uptake and nothing more. It contributes to no
verdict about whether knowledge helped or failed to help; that question needs
the randomized comparison the cohort report above is accumulating.

---

## `forge audits`

Manage the SQLite audit substrate (distinct from `forge audit`, which renders a
single audit file).

```bash
forge audits rebuild                      # rebuild the substrate from per-run JSON
forge audits show                         # render rows from the substrate
forge audits skips                        # query shape-gate skip / stuck events
forge audits export-assignment-history    # write a human-readable snapshot
```

**Subcommand flags:**

| Subcommand | Flag | Description |
|------------|------|-------------|
| `rebuild` | `--include-legacy-history` | Also backfill from `.forge/audits/history.jsonl` |
| `show` | `--slug <slug>` | Filter to a single slug (e.g. `issue-1325`) |
| `show` | `--limit <N>` | Maximum rows to render (default: 20) |
| `skips` | `--code <code>` | Filter to a single skip reason code |
| `skips` | `--issue <N>` | Filter to a single issue id |
| `skips` | `--category <cat>` | Filter to a taxonomy category |
| `skips` | `--since <ts>` / `--until <ts>` | ISO-8601 bounds on `emitted_at` |
| `skips` | `--stuck` | List repeated-block patterns (same code >= threshold times) |
| `skips` | `--threshold <N>` | Stuck-pattern threshold when `--stuck` is set (default: 3) |
| `export-assignment-history` | `--output <path>` | Output path (default: `.forge/assignment_history.yaml`) |

---

## `forge profiles`

Inspect and maintain model capability profiles (the cumulative per-model
counters adaptive routing reads).

```bash
forge profiles list                          # show current profile counters
forge profiles strength                      # declared tier/capability vs. observed dev behaviour
forge profiles reset --model <model-id>      # reset counters for one canonical model ID
```

**Subcommand flags:**

| Subcommand | Flag | Description |
|------------|------|-------------|
| `list` | `--model <id>` | Show only one canonical model ID |
| `list` | `--role <role>` | Show only one role |
| `strength` | `--config <path>` | forge.yaml to read declarations from (default: nearest above cwd) |
| `strength` | `--model <id>` | Show only one canonical model ID |
| `strength` | `--complexity <band>` | Show only one complexity band |
| `strength` | `--min-runs <N>` | Runs a band needs before a disagreement is claimed (default: 10) |
| `reset` | `--model <id>` | Canonical model ID to reset (required) |
| `reset` | `--role <role>` | Reset only one role's history |
| `reset` | `--complexity <band>` | Reset only one dev complexity bucket |
| `reset` | `--reason <text>` | Operator-supplied reason recorded in the reset audit log |

Every subcommand also accepts `--project-root <path>` (default: cwd, or the
config's project root for `strength`).

### `forge profiles strength`

A catalog entry's `tier`/`capability` is declared once and gates eligibility;
the profiles record what the model then actually did. `strength` puts the two
side by side, one row per live dev-capable model per complexity band, so a wrong
declaration can be corrected instead of merely routed around:

| status | meaning |
|--------|---------|
| `unobserved` | never selected at this band — no evidence either way, not agreement |
| `insufficient_evidence` | observed, but under `--min-runs`; too thin to argue with the declaration |
| `observed` | enough evidence, and no supported disagreement with the declaration |
| `underperforming_declaration` | observed below its declared peers by a margin the sample size supports |

A disagreement is claimed only when the band clears `--min-runs` and at least
two peers — same declared tier, same band, each clearing the floor themselves —
have observed rates of their own; the peer range and sample count print
alongside every row. Profile keys that cannot be attributed to a live
dev-capable model (legacy shorthands, role names, unresolved identities) are
listed separately rather than folded into some live model's rate, and evidence
recency is reported as unknown because profiles carry no per-key timestamp.

Attribution is by canonical model ID, transport included, and every stored key
is classified exactly once: a key is either claimed by one live dev-capable
model and counted in its rows, or it is listed as excluded. This is stricter
than the adaptive router's own matching, which resolves a candidate's history on
`(provider, model)` alone — deliberately so, since a report that argues with a
declaration must not draw on evidence it reports as unattributable. A
consequence worth knowing: evidence recorded under `openai/gpt-5.4/cli` counts
for that entry only, and `openai/gpt-5.4/api` reads as unobserved until it has
runs of its own.

The command is advisory and strictly read-only: it never edits a catalog
declaration or the profile store. Acting on a reported disagreement — editing
`tier`/`capability` in `forge.yaml` — stays the operator's call.

---

## `forge migrate-profiles`

Canonicalize legacy `model_profiles.yaml` and `assignment_history.yaml` keys to
current canonical model IDs.

```bash
forge migrate-profiles           # migrate in place
forge migrate-profiles --dry-run # print the migration report without writing
```

---

## Resume behavior

| Interrupted state | Resume behavior | Notes |
|-------------------|-----------------|-------|
| During PLAN | Reruns PLAN | Plan output is not persisted until complete |
| During DEV | Reruns DEV iter from scratch | Previous partial edits remain in the worktree |
| Failed VALIDATE (gate FAIL) | Reruns DEV with gate failure context | Normal retry path |
| Failed REVIEW parse / schema error | Reruns REVIEW | Review is re-invoked; no dev iteration consumed |
| REVIEW returned REQUEST_CHANGES | Reruns DEV with P1 findings | Normal review loop |
| Provider crashed / timed out mid-phase | Reruns the crashed phase | Safe; phases are idempotent |
| Stale worktree from previous run | Resumes from last confirmed phase | May produce unexpected results if the story changed |
| Manual human edits made to worktree | Resumes from VALIDATE | Coordinator sees the edited state |
| Escalate-gate decision recorded, next review cycle unrun | Recovers the decision and continues from it — REVIEW does not run | Use `forge review` instead when you want that cycle to run. See below |

### Choosing a re-entry path

`forge review` and `forge sprint --resume` (equally, `forge run --resume`) both
re-enter a stopped story, and they do different things:

- **`forge review`** runs the review phase against the worktree. It runs a
  review cycle; it does not consult the recorded pipeline state.
- **`forge sprint --resume`** re-enters the pipeline and *recovers* what an
  earlier attempt recorded — preflight, routing, plan review, and any
  escalate-gate decision — then continues from what that state says. If the
  recovered state is a decision, resume continues from the decision, not from
  the unfinished phase.

Those diverge whenever a story stopped with both a recorded escalate-gate
decision and a review cycle it never ran — for example: gate PASS after a fix,
review cycle 1 returned REQUEST_CHANGES, cycle 2 never ran, escalation decision
`accept` recorded.

| You want | Use | What happens |
|---|---|---|
| The unrun review cycle to execute against the fix | `forge review <story>` | Review cycle 2 runs; the recorded decision is not consulted |
| To honor the recorded pipeline state (the decision you already made) | `forge sprint --resume` / `forge run --resume` | Resume continues from `accept` and proceeds to landing; **no review runs** |

Neither is a default: choosing between them is choosing whether the work gets
reviewed. Check `forge status` first — a story in this state prints an
`outstanding:` line naming the unrun cycle and a `re-entry:` line naming what
each path would do. Whichever you run reports the same thing before it spends
anything, on its `↺ RESUME` lines — stated for the path you actually invoked.

`forge sprint --resume` / `forge run --resume`:

```
  ↺ RESUME   recovered phase record: escalation
  ↺ RESUME   recovered escalation decision accept (from run 20260807-...)
  ↺ RESUME   outstanding: REVIEW cycle 2 has not run (last verdict REQUEST_CHANGES, gate PASS)
  ↺ RESUME   this resume continues from that decision and will NOT run REVIEW —
             `forge review` runs REVIEW cycle 2 instead
```

`forge review` — same recovered state, and it is the path that runs the cycle:

```
  ↺ RESUME   recovered phase record: escalation
  ↺ RESUME   recovered escalation decision accept (from run 20260807-...)
  ↺ RESUME   outstanding: REVIEW cycle 2 has not run (last verdict REQUEST_CHANGES, gate PASS)
  ↺ RESUME   `forge review` runs REVIEW cycle 2 now —
             `forge sprint --resume` would continue from that decision and skip it
```

**Force a clean restart:**

```bash
git worktree remove .forge/worktrees/<slug> --force
git branch -D forge/<slug>
forge run stories/my-feature.md --verbose
```

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | API-mode Anthropic agents |
| `OPENAI_API_KEY` | API-mode OpenAI agents |
| `GOOGLE_API_KEY` | API-mode Google agents |
| `DEEPSEEK_API_KEY` | API-mode DeepSeek agents |
| `NTFY_URL` | Notification endpoint |
| `SLACK_WEBHOOK_URL` | Slack notifications when configured |

Set these in `.forge/.env` or as shell environment variables.

---

## See also

- [Getting Started](getting-started.md)
- [Inputs Reference](inputs-reference.md)
- [Provider Setup Guide](choose-your-provider-setup.md)
- [Troubleshooting](troubleshooting.md)
