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
```

**Use this when:** You implemented or fixed something in a worktree and want to
run review without re-running PLAN/DEV.

**Flags:**

| Flag | Description |
|------|-------------|
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
  - {issue: 123}             # source from GitHub issue #123
  - {issue: 124, slug: my-slug, depends_on: [my-slug]}
```

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
In v0.8, this is the quickest way to inspect the role table derived from `models:`.

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
forge status
```

For an active sprint run, `forge status` includes the per-story sprint status
view. The old standalone `forge sprint-status` command is no longer exposed by
the top-level parser.

---

## `forge logs`

Tail the log file for a running detached run.

```bash
forge logs <run-id>
```

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
