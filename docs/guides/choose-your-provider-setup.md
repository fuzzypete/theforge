# Provider Setup Guide

TheForge supports many provider configurations. This guide presents five named
patterns to help you pick the right setup for your situation rather than
configuring from scratch.

Every pattern is written in the canonical model shape: a model is identified by
**provider + model + transport kind** (`cli` or `api`), written either as the
identity string `<provider>/<model>/<cli|api>` or as a mapping with a
first-class `transport: {kind: ...}` object. TheForge derives the preflight,
plan, dev, review and synthesis roles from that list. See
[inputs reference](inputs-reference.md#model-identity-and-transport) for the
full schema.

> The older top-level `profiles:` block (with bare `cli:`/`provider:` keys and
> no transport object) still loads, but it is legacy: it spells dispatch as a
> pair of sibling fields instead of one transport object. Prefer `models:` for
> new configuration.

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
models:
  - anthropic/sonnet/cli        # cheaper tier — dev
  - anthropic/opus/cli          # strong tier — reviewer

budget_usd: 7.00
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

`haiku` is not in the built-in registry, so declare its identity inline. The
mapping form spells out all three parts of the identity plus its routing tier:

```yaml
models:
  enabled:
    - provider: anthropic
      model: haiku                # cheaper, faster dev iterations
      transport:
        kind: cli
      routing:
        tier: cheap
      cost:
        input_per_mtok: 0.80
        output_per_mtok: 4.00
    - anthropic/opus/cli          # strong reviewer catches what haiku misses

budget_usd: 4.00
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

Cross-provider coverage comes from listing models whose identities differ in
provider *and* transport — each entry is one identity, so the same model over
two transports would be two distinct reviewers:

```yaml
models:
  - anthropic/sonnet/cli              # dev
  - anthropic/opus/cli                # reviewer
  - openai/gpt-5.4/api                # reviewer, OpenAI API adapter
  - google/gemini-3-flash-preview/api  # reviewer, Google API adapter

budget_usd: 10.00

# Optional: pin review roles and per-reviewer thinking budgets. Reviewer names
# are the model identity with '/' replaced by '-'.
overrides:
  review_pool:
    - name: anthropic-opus-cli
      review_role: correctness        # logic bugs, spec compliance
    - name: openai-gpt-5.4-api
      review_role: patterns           # code patterns, style
    - name: google-gemini-3-flash-preview-api
      review_role: edge-cases         # edge cases, error handling
      thinking_budget: 2048           # optional: Gemini extended reasoning
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

A local model is an ordinary **API transport** pointed at a local endpoint.
Locality is endpoint metadata (`base_url`) — there is no `local` provider and no
`local` transport kind:

```yaml
models:
  enabled:
    - provider: openai              # OpenAI-compatible adapter…
      model: qwen2.5-coder:32b      # strong local coding model
      transport:
        kind: api                   # …over the API transport…
      base_url: http://localhost:11434/v1   # …pointed at a local endpoint
      routing:
        tier: fast
      cost:
        input_per_mtok: 0           # local inference is not billed per token
        output_per_mtok: 0

budget_usd: 1.00

overrides:
  dev:
    timeout_seconds: 1800           # local models are slower — increase timeout
```

A single model does both dev and review here: with one entry in `models:`, the
review pool is that same model.

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

Zero-cost local inference lands in the cheapest routing band, so it takes the
dev role and the cloud model reviews:

```yaml
models:
  enabled:
    - provider: openai
      model: qwen2.5-coder:32b
      transport:
        kind: api
      base_url: http://localhost:11434/v1
      routing:
        tier: fast
      cost:
        input_per_mtok: 0
        output_per_mtok: 0
    - anthropic/opus/cli            # strong cloud reviewer

budget_usd: 3.00

overrides:
  dev:
    timeout_seconds: 1800           # local models are slower
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
