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
run review without re-running PLAN/DEV.

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
| `--config <path>` | Path to `forge.yaml` |

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
forge profiles reset --model <model-id>      # reset counters for one canonical model ID
```

**Subcommand flags:**

| Subcommand | Flag | Description |
|------------|------|-------------|
| `list` | `--model <id>` | Show only one canonical model ID |
| `list` | `--role <role>` | Show only one role |
| `reset` | `--model <id>` | Canonical model ID to reset (required) |
| `reset` | `--role <role>` | Reset only one role's history |
| `reset` | `--complexity <band>` | Reset only one dev complexity bucket |
| `reset` | `--reason <text>` | Operator-supplied reason recorded in the reset audit log |

Both subcommands also accept `--project-root <path>` (default: cwd).

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
