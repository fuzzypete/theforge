# CLI Reference

All commands available through the `forge` CLI.

---

## `forge init`

Generate a starter `forge.yaml` and story template in the current directory.

```bash
forge init
```

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

---

## `forge review`

Run only the review phase on an existing worktree (skip plan/dev).

```bash
forge review <spec-file> [flags]
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--worktree <path>` | Explicit worktree path (default: derived from slug) |
| `--auto-merge` | Merge after APPROVE |
| `--verbose`, `-v` | Show reviewer activity |
| `--slug <name>` | Override slug |
| `--config <path>` | Path to forge.yaml |
| `--no-notify` | Suppress notifications |

**When to use:** After manually implementing a feature in a worktree and wanting
forge's multi-model review.

---

## `forge sprint`

Run multiple stories sequentially through the full pipeline.

```bash
forge sprint <manifest.yaml> [flags]
```

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

**Flags:**

| Flag | Description |
|------|-------------|
| `--output <path>` | Write generated spec to file (default: stdout) |
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
and the coordinator synthesizes converged conclusions into a spec.

---

## `forge check-providers`

Smoke-test all configured AI providers to verify connectivity and auth.

```bash
forge check-providers [flags]
```

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

**Creates:** `.forge/.env` with commented-out entries for all supported providers.
Also updates `.gitignore` to exclude `.forge/.env`.

---

## `forge audit`

Display a human-readable summary of an audit file.

```bash
forge audit <audit-file.yaml>
```

Shows: outcome, timing, cost breakdown, review verdicts, findings.

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
