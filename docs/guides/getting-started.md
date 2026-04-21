# Getting Started with TheForge

This guide walks you through your first forge run — from installation to merged
feature branch.

> **Terminology note:** The primary term is "story" — stories live in `stories/`
> by default, but the directory name is a filesystem convention, not
> a different concept. Sprints run multiple stories sequentially. Briefs are
> free-form ideation inputs (not stories). The review pool is the set of models
> that review each implementation.

## What you need

1. **Python 3.11+**
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

# v0.8 simple config: list your models, set a budget, done.
models:
  - claude/sonnet    # cheap tier  — dev for small stories
  - claude/opus      # strong tier — dev for large stories, reviewers

budget_usd: 30.0

workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} main"
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

When preflight classifies a story as small/medium/large, TheForge
automatically selects the appropriate model tier for each phase:

| Phase | small | medium | large |
|-------|-------|--------|-------|
| dev | cheap | mid | strong |
| plan | mid | strong | strong |
| preflight | fast/cheap | (static) | (static) |
| code review | 1 mid reviewer | pool + synthesis | pool + synthesis |

This is the v0.8 behavior described in [#807](https://github.com/fuzzypete/theforge/issues/807).
Run `forge check-config` to see the full derived role table for your model list.

### Advanced: partial overrides

If you need to override a specific role without leaving simple mode, use the
`overrides:` key instead of the classic `profiles:` key:

```yaml
models:
  - claude/sonnet
  - claude/opus

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
pytest_target: tests/
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
forge run stories/my-feature.md --verbose
```

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

# See the full audit trail (worktree copy)
cat .forge/worktrees/add-health-check/forge_audit.yaml
# Or the persistent audit in .forge/audits/
cat .forge/audits/forge_audit.yaml
```

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

Stories run sequentially. Each one goes through the full pipeline. Failed stories
don't block the rest.

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
  "verdict": "APPROVE | REQUEST_CHANGES | ESCALATE",
  "slug": "my-feature",
  "branch": "feat/my-feature",
  "summary": "one-line review summary",
  "findings": [
    {
      "severity": "P1 | P2",
      "file": "src/foo.py",
      "line": 42,
      "description": "what is wrong",
      "suggestion": "how to fix"
    }
  ]
}
```

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

A full view of every file TheForge touches and who owns it.

```
your-project/
├── forge.yaml                         # USER — project config (models, budgets, gate)
├── stories/                           # USER — story inputs
│   ├── TEMPLATE.md                    # USER — annotated story template (from forge init)
│   └── my-feature.md                  # USER — your stories
├── sprints/                           # USER — sprint manifests
├── briefs/                            # USER — ideation inputs for forge ideate
│
├── .forge/
│   ├── .env                           # USER — API keys (auto-gitignored)
│   ├── hooks/                         # USER — lifecycle hook scripts
│   │   └── post_run.sh                # USER — (from forge init-hooks)
│   ├── logs/                          # GENERATED — per-run log files
│   ├── audits/                        # GENERATED — persistent audit trail
│   │   └── forge_audit.yaml           # GENERATED — latest run audit (overwritten per run)
│   └── worktrees/                     # GENERATED — managed git worktrees
│       └── my-feature/                # GENERATED — one per story run
│           ├── <all repo files>       # GENERATED — full worktree copy on feature branch
│           └── forge_audit.yaml       # GENERATED — worktree copy of audit trail
```

### Ownership and lifecycle

| Entry | Owner | Safe to delete? | When? |
|-------|-------|----------------|-------|
| `forge.yaml` | You | No | — |
| `stories/`, `sprints/`, `briefs/` | You | No | — |
| `.forge/.env` | You | No — contains secrets | — |
| `.forge/hooks/` | You | Yes | If you don't use hooks |
| `.forge/logs/` | Generated | Yes | After reviewing |
| `.forge/audits/forge_audit.yaml` | Generated | Yes | Overwritten each run |
| `.forge/worktrees/<slug>/` | Generated | Yes | After merge or abandonment |
| `.forge/worktrees/<slug>/forge_audit.yaml` | Generated | Yes | After merge or abandonment |

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
