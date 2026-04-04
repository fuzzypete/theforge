# Provider Setup Guide

TheForge supports many provider configurations. This guide presents five named
patterns to help you pick the right setup for your situation rather than
configuring from scratch.

---

## Quick decision

| I want... | Use pattern |
|-----------|-------------|
| The fastest possible start | [Fastest start](#1-fastest-start) |
| Lower cost for dev, stronger review | [Budget-conscious](#2-budget-conscious) |
| Maximum bug-catching coverage | [Maximum coverage](#3-maximum-coverage) |
| Full privacy / no cloud calls | [Local / privacy-first](#4-local--privacy-first) |
| Mix of local dev + cloud review | [Hybrid local/cloud](#5-hybrid-localcloud) |

---

## 1. Fastest start

**When to use:** You have Claude Code CLI installed and want to run your first
story today. One model, zero API key setup.

**Cost per story:** ~$1-3

**Trade-offs:**
- Speed: fast (single model, no pool overhead)
- Cost: moderate
- Quality: good (Opus reviewer)
- Privacy: cloud calls to Anthropic

```yaml
profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 5.00
    timeout_seconds: 600
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
  review_pool:
    - name: claude-reviewer
      cli: claude
      model: opus
      budget_usd: 2.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]
```

**Prerequisites:** [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
installed and authenticated.

---

## 2. Budget-conscious

**When to use:** You're running many stories and want to control costs without
sacrificing review quality. Use a cheaper model for dev and a stronger model
for review.

**Cost per story:** ~$0.50-1.50

**Trade-offs:**
- Speed: moderate (Haiku is faster, Opus review is thorough)
- Cost: low
- Quality: good (weaker dev, but review catches issues)
- Privacy: cloud calls to Anthropic

```yaml
profiles:
  dev:
    cli: claude
    model: haiku          # cheaper, faster dev iterations
    budget_usd: 2.00
    timeout_seconds: 600
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
  review_pool:
    - name: claude-reviewer
      cli: claude
      model: opus           # strong reviewer catches what haiku misses
      budget_usd: 2.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]
```

**Prerequisites:** Claude Code CLI.

---

## 3. Maximum coverage

**When to use:** Production-grade code where you want multiple independent
perspectives. Different models catch different bug classes.

**Cost per story:** ~$4-10 (dev + 3-4 reviewers)

**Trade-offs:**
- Speed: slower (parallel review pool, but wall-clock is ~review time)
- Cost: higher
- Quality: excellent (multi-model cross-review)
- Privacy: cloud calls to multiple providers

```yaml
profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 5.00
    timeout_seconds: 600
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
  review_pool:
    - name: claude-reviewer
      cli: claude
      model: opus
      review_role: correctness      # logic bugs, spec compliance
      budget_usd: 2.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]
    - name: codex-reviewer
      provider: openai              # API mode
      model: o4-mini
      review_role: patterns         # code patterns, style
      budget_usd: 1.00
      timeout_seconds: 300
    - name: gemini-reviewer
      provider: google              # API mode
      model: gemini-2.5-flash
      review_role: edge-cases       # edge cases, error handling
      thinking_budget: 2048         # optional: enables Gemini extended reasoning
      budget_usd: 1.00
      timeout_seconds: 300
```

**Prerequisites:**
- Claude Code CLI
- `forge secrets-init` + set `OPENAI_API_KEY` and `GOOGLE_API_KEY` in `.forge/.env`

---

## 4. Local / privacy-first

**When to use:** Your code can't leave the machine (compliance, IP sensitivity,
air-gapped environments). Uses Ollama for fully local inference.

**Cost per story:** ~$0 (electricity only)

**Trade-offs:**
- Speed: slow (local models are much slower than cloud APIs)
- Cost: free (after hardware)
- Quality: lower (local models lag cloud frontier)
- Privacy: all inference stays on your machine

```yaml
profiles:
  dev:
    provider: openai
    model: qwen2.5-coder:32b      # strong local coding model
    base_url: http://localhost:11434/v1
    budget_usd: 0                  # no cost tracking for local
    timeout_seconds: 1800          # local models are slower — increase timeout
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
  review_pool:
    - name: local-reviewer
      provider: openai
      model: qwen2.5-coder:32b
      base_url: http://localhost:11434/v1
      budget_usd: 0
      timeout_seconds: 900
      allowed_tools: [Read, Bash, Glob, Grep]
```

**Prerequisites:**
- [Ollama](https://ollama.com) installed and running (`ollama serve`)
- Model pulled: `ollama pull qwen2.5-coder:32b`

See [Local Models Guide](local-models.md) for setup details and alternative models.

---

## 5. Hybrid local/cloud

**When to use:** You want fast/cheap local dev with cloud-quality review, or
local review for privacy with cloud dev for speed.

**Cost per story:** ~$0.50-1.00 (review only)

**Trade-offs:**
- Speed: moderate (local dev is slower; cloud review is fast)
- Cost: low (only review hits the cloud)
- Quality: good-to-excellent depending on local model
- Privacy: dev stays local; review uses cloud

```yaml
profiles:
  dev:
    provider: openai
    model: qwen2.5-coder:32b
    base_url: http://localhost:11434/v1
    budget_usd: 0
    timeout_seconds: 1800
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]
  review_pool:
    - name: claude-reviewer
      cli: claude
      model: opus                   # strong cloud reviewer
      budget_usd: 2.00
      timeout_seconds: 300
      allowed_tools: [Read, Bash, Glob, Grep]
```

**Prerequisites:**
- Ollama installed and running
- Claude Code CLI installed and authenticated

---

## API keys setup

For patterns using API-mode providers (OpenAI, Google, DeepSeek):

```bash
forge secrets-init         # creates .forge/.env (gitignored)
```

Edit `.forge/.env`:
```bash
OPENAI_API_KEY=sk-proj-...
GOOGLE_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
```

CLI-mode providers (Claude Code, Codex CLI, Gemini CLI) handle their own
authentication — no keys needed in `.forge/.env`.

---

## Verify your setup

```bash
forge check-config
forge check-providers
```

Run `forge check-config` after any config change, then `forge check-providers`
to confirm your API-mode models respond correctly.

---

## See also

- [Getting Started](getting-started.md) — full install and config walkthrough
- [Local Models Guide](local-models.md) — Ollama and vLLM setup details
- [CLI Reference](cli-reference.md) — all commands and flags
- [Troubleshooting](troubleshooting.md) — provider auth and connectivity issues
