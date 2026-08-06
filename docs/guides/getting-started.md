# Getting Started with TheForge

This guide walks you through your first forge run — from installation to merged
feature branch.

> **Terminology note:** The primary term is "story" — stories live in `stories/`
> by default, but the directory name is a filesystem convention, not
> a different concept. Sprints run multiple stories sequentially. Briefs are
> free-form ideation inputs (not stories). The review pool is the set of models
> that review each implementation.

## What you need

1. **Python 3.12+**
2. **At least one AI CLI** — [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
   is recommended to start. Codex and Gemini are optional.
3. **A git repository** with tests

> **Tip:** You don't need all four providers to get started. A single Claude CLI
> handles both dev and review. Add Codex/Gemini/DeepSeek reviewers later for
> cross-model coverage.

## 1. Install TheForge

```bash
git clone https://github.com/fuzzypete/theforge.git
cd theforge
pip install -e .
```

Verify it works:

```bash
forge --help
```

## 2. Initialize your project

```bash
cd /path/to/your-project
forge init
```

This creates:
- `forge.yaml` — project configuration (models, budgets, timeouts)
- `stories/TEMPLATE.md` — annotated story template
- `.gitignore` entry for `.forge/.env`

## 3. Edit forge.yaml

The minimal config is a model list and a budget. TheForge figures out role
assignment automatically — dev, preflight, plan, and review pool are all
derived from the list using **complexity-aware routing** (see below).

```yaml
project: my-project

# Simple config: list your models, set a budget, done.
models:
  - anthropic/sonnet/cli   # cheap tier  — dev for small stories
  - anthropic/opus/cli     # strong tier — dev for large stories, reviewers

budget_usd: 30.0

workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} {base_branch}"
  setup_command: "pip install -e ."    # your dependency install command
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  gate_command: "python -m pytest tests/ -q"   # your test command
```

**Key things to customize:**
- `models` — list of model keys (`provider/name`). TheForge sorts them into
  cheap/mid/strong tiers and picks the right one based on story complexity.
- `budget_usd` — total budget per story, distributed across all roles.
- `setup_command` — how to install dependencies in a fresh worktree
  (`npm install`, `poetry install`, `pip install -e .`, etc.)
- `gate_command` — your test/lint command. Must exit 0 on success.

### How complexity-aware routing works

Preflight assigns each story a complexity score (1–10). The score selects the
dev/plan model tier, reviewer count, and reasoning effort through coarse
buckets, and adaptive assignment then picks preferred models within the
eligible pool from recorded run evidence. See the
[Routing Policy guide](routing-policy.md) for what the score controls and the
[Adaptive Assignment guide](adaptive-assignment.md) for how preference,
exploration, and taint exclusion work.
Run `forge check-config` to see the full derived role table for your model list.

### Advanced: partial overrides

If you need to override a specific role without leaving simple mode, use the
`overrides:` key instead of the classic `profiles:` key:

```yaml
models:
  - anthropic/sonnet/cli
  - anthropic/opus/cli

budget_usd: 30.0

overrides:
  dev:
    timeout_seconds: 1200   # extend dev timeout for large repos
```

The classic `profiles:` key still works and is supported indefinitely for
configs that need full manual control.

## 4. Set up API keys (optional)

If you want API-mode reviewers (OpenAI, Google, DeepSeek), create a secrets
file:

```bash
forge secrets-init
```

This creates `.forge/.env` (gitignored). Edit it:

```bash
# .forge/.env
OPENAI_API_KEY=sk-proj-...
GOOGLE_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
```

> For CLI-mode agents (Claude Code, Codex CLI, Gemini CLI), authentication is
> handled by each CLI's own setup — no keys needed in `.forge/.env`.

## 5. Validate the setup

```bash
forge check-config
forge check-providers
```

`forge check-config` is the fastest way to catch config drift, missing auth,
and deprecated settings before spending tokens on a real run.

For this repo's own development workflow, `make gate` now scrubs agent credentials,
CLI auth state, and dotenv-related inputs before running tests. That makes a green
local gate match CI more closely: if a test forgot to mock a runner or only passes
because your shell is authenticated, it should fail locally too. Real-credential
checks belong in the opt-in `make test-integration` suite instead.

## 6. Write your first story

Copy `stories/TEMPLATE.md` to `stories/my-feature.md` and fill it in:

```markdown
---
name: "Add health check endpoint"
slug: add-health-check
---

# Add Health Check Endpoint

## Problem

The app has no way to verify it's running. We need a `/health` endpoint for
load balancer probes.

## Acceptance criteria

- GET `/health` returns `{"status": "ok"}` with HTTP 200
- A test verifies the endpoint returns the expected response
- Existing tests continue to pass
```

**Story writing tips:**
- **Name:** Short, human-readable. Shows up in logs and audit.
- **Slug:** Becomes the branch name (`forge/add-health-check`) and worktree path.
  Use lowercase-with-dashes.
- **Acceptance criteria:** These are the dev agent's checklist. Be specific and
  testable. "The system does X when Y" is better than "implement X."
- **Problem section:** Context for the agent, but NOT requirements. The agent
  implements ACs, not prose.

## 7. Run it

```bash
forge run stories/my-feature.md --verbose --fg
```

> **Runs detach by default.** Without `--fg`, `forge run` returns immediately
> and the run continues in the background — use `forge status` to check
> progress and `forge logs` to tail output. `--fg` keeps the run in the
> foreground so you can watch it live, which is what this walkthrough assumes.

You'll see the pipeline in real time:

```
[forge] ▸ WORKSPACE   add-health-check
[forge] ▸ PREFLIGHT   sonnet
[forge]   Verdict: PROCEED
[forge] ▸ DEV         sonnet  iter 1
[forge]   ↳ Read: src/app.py
[forge]   ↳ Edit: src/app.py
[forge]   ↳ Write: tests/test_health.py
[forge] ▸ VALIDATE    pytest
[forge]   Gate: PASS (12 passed in 0.8s)
[forge] ▸ REVIEW      opus
[forge]   ✓ REVIEW   APPROVE  0 P1  $1.23  3m 42s
[forge] ▸ DONE        add-health-check
```

## 8. Inspect the results

The implementation lives on a feature branch:

```bash
# See what was created
git log forge/add-health-check --oneline

# See the full audit trail for the last run
cat .forge/audits/forge_audit.yaml
# Or the per-run record
cat .forge/audits/runs/<run_id>.json
```

`forge explain` summarizes the last run's decisions, and `forge audits show`
queries the audit history. (A worktree copy at
`.forge/worktrees/<slug>/forge_audit.yaml` is written only when a run
ESCALATEs, to preserve state for diagnosis.)

The audit shows:
- Every phase timing and cost
- Preflight verdict and reasoning
- Gate decisions (PASS/FAIL)
- Review verdict, findings, and reviewer breakdown
- Total cost and duration

## 9. Merge (optional)

```bash
# Auto-merge on the next run
forge run stories/my-feature.md --auto-merge

# Or merge manually
git merge forge/add-health-check
```

## 10. Run multiple stories as a sprint

### Option A: manifest file

Create `sprints/my-sprint.yaml`:

```yaml
name: "My First Sprint"
budget_usd: 20
stories:
  - stories/add-health-check.md
  - stories/add-logging.md
  - stories/fix-auth-bug.md
```

Run it:

```bash
forge sprint sprints/my-sprint.yaml --verbose --auto-merge
```

### Option B: paperless sprint from GitHub

If your stories are GitHub issues, skip the manifest entirely:

```bash
forge sprint --milestone "v1.0" --budget 50 --verbose --auto-merge
forge sprint --label "sprint-1" --budget 20 --verbose
```

TheForge fetches open issues from the milestone or label and runs them in order.

### Ordering and parallelism

Stories run in DAG order, not strictly sequentially. Edges come from two
sources: explicit `depends_on` entries in the manifest, and automatic
collision-derived edges — when preflight predicts two stories will touch the
same files (`likely_files`), the later story waits for the earlier one.
`--parallel N` runs up to N independent stories concurrently. Each story goes
through the full pipeline, and failed stories don't block unrelated ones. See
the [Inputs Reference](inputs-reference.md) for manifest semantics.

## What things cost

Typical costs per spec (varies by complexity):

| Complexity | Dev (Sonnet) | Review (Opus) | Total |
|-----------|-------------|--------------|-------|
| Small (1-2 files) | $0.50-1.50 | $0.30-0.80 | ~$1-2 |
| Medium (3-8 files) | $1.50-4.00 | $0.50-1.50 | ~$2-6 |
| Large (8+ files) | $3.00-8.00 | $1.00-3.00 | ~$5-12 |

Adding more reviewers increases review cost proportionally but catches more bugs.
A 4-model review pool (Claude + Codex + Gemini + DeepSeek) costs ~$2-4 per review
but provides excellent cross-model coverage.

## Lifecycle hooks

Hooks let you run arbitrary shell scripts at key forge events. The most useful
hook is `post_run`, which fires after every `forge run` and receives a JSON
payload describing the outcome.

### Scaffold the reference hook

```bash
forge init-hooks
```

This creates `.forge/hooks/post_run.sh` — a reference script that files GitHub
Issues for P1/P2 findings on ESCALATE or APPROVE outcomes. It requires the
`gh` CLI (authenticated) and `jq`.

### Activate the hook in forge.yaml

```yaml
hooks:
  post_run: .forge/hooks/post_run.sh
  timeout_seconds: 30
```

### Hook payload (stdin JSON)

```json
{
  "event": "post_run",
  "project": "my-project",
  "slug": "my-feature",
  "branch": "forge/my-feature",
  "run_id": "20260805-120000-my-feature",
  "outcome": "done | escalate",
  "verdict": "APPROVE | REQUEST_CHANGES | ESCALATE",
  "summary": "one-line review summary",
  "total_cost_usd": 1.23,
  "duration_seconds": 222.0,
  "findings": [
    {
      "severity": "P1 | P2",
      "file": "src/foo.py",
      "line": 42,
      "observed": "what the code does",
      "expected": "what it should do",
      "evidence": "why the reviewer believes this",
      "suggestion": "how to fix"
    }
  ]
}
```

The payload also carries `story`, `cycles`, `dev_iterations`, `gate_decisions`,
`gate_runs`, `review_pool`, `pr_number`, and per-reviewer `reviewers`
attribution — pipe stdin through `jq .` in a hook to see the full shape.

Hooks exit 0 to signal success. A non-zero exit prints a warning but does not
abort the forge run. See `.forge/hooks/README.md` for full documentation.

## Next steps

- **Add more reviewers:** See [Provider Setup Guide](choose-your-provider-setup.md) for named patterns
- **Enable plan phase:** For medium/large stories, add `plan:` to forge.yaml
  to have an agent create an implementation plan before dev starts
- **Generate stories from briefs:** Use `forge ideate "problem description"` to
  have multiple models collaboratively write a story
- **Check config health:** Run `forge check-config` after config edits
- **Check provider health:** Run `forge check-providers` to verify your
  API-mode models respond correctly
- **File findings as issues:** Run `forge init-hooks` to scaffold the GitHub
  Issues hook
- **Something went wrong?** See the [Troubleshooting guide](troubleshooting.md)
- **See a narrated transcript:** Read the [First-Run Walkthrough](first-run-walkthrough.md)

---

## What gets created

`forge.yaml`, `stories/`, `sprints/`, and `briefs/` at your repo root are
yours — TheForge never rewrites them. Everything TheForge generates lives
under `.forge/`.

`.forge/` contents fall into four categories — secrets, machine-local
runtime state, derived views, and project memory — rather than a fixed
file list, because new subdirectories get added as TheForge grows. What
each category means, whether it travels with the repo, and what
committing the wrong one would break is covered in the dedicated
[Storage Layout guide](forge-storage.md), which also explains the
shared-memory vs. local-memory choice you make at `forge init` time and
points at the canonical `.gitignore`/`.gitattributes` template as the
single source of truth for exactly what's tracked.

### Mental model

> TheForge is a **coordinator**, not an autonomous IDE.

- **Each phase has a narrow job** — workspace setup, preflight check, dev,
  validate, review. No phase does more than one thing.
- **Models produce artifacts, not runtime authority** — the dev agent writes
  code. The coordinator decides what happens next.
- **Validation and review are gates, not suggestions** — a FAIL gate stops
  the run; a P1 finding triggers a retry. This is mechanical, not advisory.
- **Your repo is always safe** — all agent work happens in a worktree on a
  feature branch. Main is untouched until you explicitly merge.
- **Defaults are conventions, not constraints** — `stories/` and `forge/{slug}`
  are starter defaults, but both story paths and branch patterns are configurable.
