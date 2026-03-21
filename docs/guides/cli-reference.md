# CLI Reference

All commands available through the `forge` CLI.

---

## `forge init`

Generate a starter `forge.yaml` and story template in the current directory.

```bash
forge init
```

**Use this when:** Starting a new project with TheForge for the first time.
**Avoid this when:** You already have a `forge.yaml` — it will overwrite it.

**Creates:**
- `forge.yaml` — starter config with Claude dev + review
- `specs/TEMPLATE.md` — annotated story template
- `.gitignore` entry for `.forge/.env`

---

## `forge run`

Execute the full pipeline (plan → dev → validate → review) for a single story.

```bash
forge run <spec-file> [flags]
```

**Use this when:** You have a single story ready and want end-to-end execution.
**Avoid this when:** Running many stories — use `forge sprint` instead.
**Pairs well with:** `--verbose` on first use to see what's happening; `--auto-merge` for unattended runs.

**Flags:**

| Flag | Description |
|------|-------------|
| `--verbose`, `-v` | Show tool activity and raw agent output in real time |
| `--auto-merge` | Merge feature branch to main after APPROVE |
| `--interactive` | Pause at APPROVE for human confirmation |
| `--resume` | Detect existing worktree state and resume from the right phase |
| `--plan <path>` | Inject a pre-written plan, skip PLAN phase |
| `--slug <name>` | Override slug from spec frontmatter |
| `--config <path>` | Path to forge.yaml (default: walk up from spec) |
| `--dry-run` | Print prompts without invoking agents |
| `--no-notify` | Suppress OS/ntfy notifications |

**Examples:**

```bash
# Basic run
forge run specs/add-auth.md --verbose

# Resume a failed run from where it left off
forge run specs/add-auth.md --resume --verbose

# Inject a pre-written plan
forge run specs/add-auth.md --plan docs/plans/auth-plan.md

# Dry run — see what prompts would be sent
forge run specs/add-auth.md --dry-run
```

### Flag guidance

**`--resume`**
**Use this when:** A run was interrupted (crash, Ctrl+C, timeout) and you want
to continue without losing the work already done.
**Avoid this when:** The worktree was manually edited in ways that might confuse
state detection — delete the worktree and start fresh instead.
**Pairs well with:** `--verbose` to verify which phase it resumed from.

See [Resume Behavior](#resume-behavior) for the full state recovery matrix.

---

**`--verbose`**
**Use this when:** Diagnosing provider issues, gate failures, or worktree behavior;
or any time you want to see agent tool calls in real time.
**Avoid this when:** Running unattended in CI — use log files instead.
**Pairs well with:** `--resume` when diagnosing a resume scenario.

---

**`--auto-merge`**
**Use this when:** You trust the review gate and want fully unattended runs.
**Avoid this when:** You want to inspect the diff before merging.
**Pairs well with:** `forge sprint` for batch unattended runs.

---

**`--interactive`**
**Use this when:** You want a human checkpoint before merge even after APPROVE.
**Avoid this when:** Unattended/CI runs — it will block indefinitely waiting for input.
**Pairs well with:** `--verbose` to see what you're approving.

---

**`--dry-run`**
**Use this when:** You want to inspect the prompts that will be sent before
committing real API spend; or when writing a new story and validating it reads correctly.
**Avoid this when:** You actually want to run the pipeline.
**Pairs well with:** Any story you're writing for the first time.

---

## `forge review`

Run only the review phase on an existing worktree (skip plan/dev).

```bash
forge review <spec-file> [flags]
```

**Use this when:** You've manually implemented a feature in a worktree and want
multi-model review, or a previous review failed to parse and you want to retry.
**Avoid this when:** You haven't run dev yet — there's nothing to review.
**Pairs well with:** `--verbose` to see the reviewer's reading activity.

**Flags:**

| Flag | Description |
|------|-------------|
| `--worktree <path>` | Explicit worktree path (default: derived from slug) |
| `--auto-merge` | Merge after APPROVE |
| `--verbose`, `-v` | Show reviewer activity |
| `--slug <name>` | Override slug |
| `--config <path>` | Path to forge.yaml |
| `--no-notify` | Suppress notifications |

---

## `forge sprint`

Run multiple stories sequentially through the full pipeline.

```bash
forge sprint <manifest.yaml> [flags]
```

**Use this when:** You have a batch of related stories to run — e.g., all stories
in a milestone, or an end-of-day unattended run.
**Avoid this when:** Stories have dependencies on each other that aren't tracked
via `depends_on` in the manifest — they may conflict.
**Pairs well with:** `--auto-merge` and `--resume` for reliable unattended batch runs.

**Flags:**

| Flag | Description |
|------|-------------|
| `--verbose`, `-v` | Show activity for all stories |
| `--auto-merge` | Auto-merge each approved story |
| `--interactive` | Pause at each APPROVE |
| `--resume` | Auto-triage each story and resume from correct phase |
| `--config <path>` | Path to forge.yaml |
| `--no-notify` | Suppress notifications |

**Sprint manifest format:**

```yaml
name: "Sprint Name"
budget_usd: 50          # total budget across all stories
auto_merge: true         # auto-merge each APPROVED story (optional)
specs:
  - specs/story-one.md
  - specs/story-two.md
  - specs/story-three.md
```

Stories run in order. A failed story doesn't block subsequent ones. Budget is
shared — if story-one uses $30 of a $50 budget, story-two gets $20.

---

## `forge ideate`

Multi-LLM deliberation to generate a story from a problem description.

```bash
forge ideate <brief-text-or-file> [flags]
```

**Use this when:** You have a fuzzy idea and want a well-structured story with
acceptance criteria before running the pipeline.
**Avoid this when:** You already have clear acceptance criteria — write the story
directly.
**Pairs well with:** Saving output to `specs/` and reviewing it before running.

**Flags:**

| Flag | Description |
|------|-------------|
| `--output <path>` | Write generated story to file (default: stdout) |
| `--rounds <N>` | Deliberation rounds, 1-3 (default: 2) |
| `--config <path>` | Path to forge.yaml |
| `--dry-run` | Print to stdout, don't write file |
| `--verbose`, `-v` | Show deliberation activity |

**Examples:**

```bash
# From inline text
forge ideate "Add rate limiting to the API" --output specs/rate-limiting.md

# From a brief file
forge ideate briefs/rate-limiting.txt --output specs/rate-limiting.md
```

Multiple models generate ideas independently, cross-review each other's output,
and the coordinator synthesizes converged conclusions into a story.

---

## `forge check-providers`

Smoke-test all configured AI providers to verify connectivity and auth.

```bash
forge check-providers [flags]
```

**Use this when:** Before the first run on a new machine, after rotating API keys,
or when debugging provider errors.
**Avoid this when:** — (always safe to run)
**Pairs well with:** Running immediately after `forge init` or after changing `forge.yaml`.

**Flags:**

| Flag | Description |
|------|-------------|
| `--profile <name>` | Test only one specific profile |
| `--config <path>` | Path to forge.yaml |

**Example output:**

```
[check] Testing 4 provider(s)...
  ✓ claude-reviewer (claude/opus)         0.8s
  ✓ codex-reviewer (openai/o4-mini)       1.2s
  ✓ gemini-reviewer (google/gemini-2.5-flash)  0.6s
  ✗ deepseek-reviewer (deepseek/deepseek-chat)  error: 401 Unauthorized
[check] 3 of 4 providers healthy
```

---

## `forge secrets-init`

Create a `.forge/.env` skeleton for API keys.

```bash
forge secrets-init
```

**Use this when:** Setting up API-mode providers (OpenAI, Google, DeepSeek).
**Avoid this when:** Using only CLI-mode providers (Claude Code, Codex CLI, Gemini CLI)
— they handle auth themselves, no `.forge/.env` needed.

**Creates:** `.forge/.env` with commented-out entries for all supported providers.
Also updates `.gitignore` to exclude `.forge/.env`.

---

## `forge audit`

Display a human-readable summary of an audit file.

```bash
forge audit <audit-file.yaml>
```

**Use this when:** Reviewing a run's outcome, cost breakdown, or reviewer findings.
**Avoid this when:** — (always safe to run, read-only)

Shows: outcome, timing, cost breakdown, review verdicts, findings.

---

## Resume behavior

| Interrupted state | Resume behavior | Notes |
|-------------------|-----------------|-------|
| During PLAN | Reruns PLAN | Plan output not persisted until complete |
| During DEV | Reruns DEV iter from scratch | Previous partial edits are in worktree |
| Failed VALIDATE (gate FAIL) | Reruns DEV with gate failure context | Normal retry path |
| Failed REVIEW parse / schema error | Reruns REVIEW | Review is re-invoked; no dev iteration consumed |
| REVIEW returned REQUEST_CHANGES | Reruns DEV with P1 findings | Normal review loop |
| Provider crashed / timed out mid-phase | Reruns the crashed phase | Safe; phases are idempotent |
| Stale worktree from previous run | Resumes from last confirmed phase | May produce unexpected results if story changed |
| Manual human edits made to worktree | Resumes from VALIDATE | Coordinator sees edited state |

**Force a clean restart:**
```bash
git worktree remove .forge/worktrees/<slug> --force
git branch -D forge/<slug>
forge run specs/my-feature.md --verbose   # no --resume
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

Set these in `.forge/.env` (project-scoped) or as shell environment variables.

---

## See also

- [Troubleshooting](troubleshooting.md) — common errors and fixes
- [Getting Started](getting-started.md) — initial setup walkthrough
- [Inputs Reference](inputs-reference.md) — story and sprint file formats
- [Provider Setup Guide](choose-your-provider-setup.md) — which provider pattern to use
