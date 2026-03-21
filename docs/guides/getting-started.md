# Getting Started with TheForge

This guide walks you through your first forge run — from installation to merged
feature branch.

> **Terminology note:** The primary term is "story" — stories live in `specs/`
> by convention, but the `specs/` directory name is a filesystem convention, not
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
- `specs/TEMPLATE.md` — annotated story template
- `.gitignore` entry for `.forge/.env`

## 3. Edit forge.yaml

The generated config uses Claude for both dev and review. Adjust to match your
project:

```yaml
project: my-project

workspace:
  create_command: "git worktree add .forge/worktrees/{slug} -b forge/{slug} main"
  setup_command: "pip install -e ."    # your dependency install command
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "forge/{slug}"

validation:
  gate_command: "python -m pytest tests/ -q"   # your test command
  handoff_file: "handoff.yaml"
  gate_decision_key: "gate_decision"
```

**Key things to customize:**
- `setup_command` — how to install dependencies in a fresh worktree
  (`npm install`, `poetry install`, `pip install -e .`, etc.)
- `gate_command` — your test/lint command. Must exit 0 on success.
- `budget_usd` — cost ceiling per agent. Start low ($2-5) while learning.

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

## 5. Write your first story

Copy `specs/TEMPLATE.md` to `specs/my-feature.md` and fill it in:

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

## 6. Run it

```bash
forge run specs/my-feature.md --verbose
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

## 7. Inspect the results

The implementation lives on a feature branch:

```bash
# See what was created
git log forge/add-health-check --oneline

# See the full audit trail
cat .forge/worktrees/add-health-check/forge_audit.yaml
```

The audit shows:
- Every phase timing and cost
- Preflight verdict and reasoning
- Gate decisions (PASS/FAIL)
- Review verdict, findings, and reviewer breakdown
- Total cost and duration

## 8. Merge (optional)

```bash
# Auto-merge on the next run
forge run specs/my-feature.md --auto-merge

# Or merge manually
git merge forge/add-health-check
```

## 9. Run multiple specs as a sprint

Create a sprint manifest (`sprints/my-sprint.yaml`):

```yaml
name: "My First Sprint"
budget_usd: 20
specs:
  - specs/add-health-check.md
  - specs/add-logging.md
  - specs/fix-auth-bug.md
```

Run it:

```bash
forge sprint sprints/my-sprint.yaml --verbose --auto-merge
```

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

- **Add more reviewers:** See [CLI Reference](cli-reference.md) for multi-model
  review pool setup
- **Enable plan phase:** For medium/large stories, add `plan:` to forge.yaml
  to have an agent create an implementation plan before dev starts
- **Generate stories from briefs:** Use `forge ideate "problem description"` to
  have multiple models collaboratively write a story
- **Check provider health:** Run `forge check-providers` to verify all your
  configured models respond correctly
- **File findings as issues:** Run `forge init-hooks` to scaffold the GitHub
  Issues hook
