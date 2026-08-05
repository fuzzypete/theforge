# Model Reference

> **Dated guidance.** These recommendations reflect vendor positioning as of
> **April 2026**, and the registry/tier surface described here is being
> replaced by a unified model catalog (#2204). Treat the tables as historical
> guidance; the repo's `forge.yaml` and
> [`routing-policy.md`](routing-policy.md) are current.

Per-provider model recommendations for TheForge, organized by phase and
complexity tier.

TheForge's adaptive assignment maps complexity to routing tiers
(`cheap`/`mid`/`strong`) through the current routing policy described in
[`adaptive-assignment.md`](adaptive-assignment.md) and
[`routing-policy.md`](routing-policy.md). The tables below show which concrete
model fits each tier cell. Note there are two tier vocabularies: the registry's
`routing.tier` field uses `cheap`/`fast`/`strong` (a speed/latency band), while
the adaptive pool tiers are derived from `cost_rank` as `cheap`/`mid`/`strong`
(`config/profiles.py`). This doc's tier mappings use the adaptive pool
vocabulary (`cheap`/`mid`/`strong`).

The shipped model set itself is **data**: `src/theforge/config/data/models.yaml`
inside the package, written in the canonical model-definition schema documented
in [`inputs-reference.md`](inputs-reference.md#model-definitions) and read by the
same parser that reads a project's `forge.yaml`. Adding or re-pinning a model
that runs on an existing adapter is an edit to that file — or to your own
`forge.yaml`, with no code change at all. Adding a *provider* still requires
code, because a provider needs a runner module.

> **TheForge's own `forge.yaml` is deliberately unchanged.** Moving this
> repository's config onto the canonical schema is separate operator work,
> sequenced after a release containing the schema has been cut and deployed:
> sprints here run against an *installed release*, which cannot read a schema it
> does not yet contain.

---

## OpenAI (GPT-5.4 family)

Source: Codex recommendation, April 2026.

| Phase | Small | Medium | Large |
|---|---|---|---|
| Preflight | `gpt-5.4-mini` effort=medium | `gpt-5.4` effort=medium | `gpt-5.4` effort=high |
| Planning | `gpt-5.4-mini` effort=medium | `gpt-5.4` effort=high | `gpt-5.4-pro` effort=high |
| Dev | `gpt-5.4-mini` effort=low | `gpt-5.4` effort=medium | `gpt-5.4-pro` effort=high |
| Review | `gpt-5.4-mini` effort=medium | `gpt-5.4` effort=medium | `gpt-5.4` effort=high |

**Tier mapping:**
- **cheap:** `gpt-5.4-mini` — fast, cost-effective for small stories and high-volume work
- **mid:** `gpt-5.4` — flagship for complex reasoning and coding; default backbone
- **strong:** `gpt-5.4-pro` — reserved for large planning and dev where "think harder" justifies latency and cost

**Notes:**
- `o4-mini` is retired; GPT-5.4 family is the current default line
- `gpt-5.4-pro` should not be used as a reviewer — cost doesn't justify it for review workloads
- `reasoning_effort` is a first-class config key in forge.yaml profile definitions.
  Left unset, it is now resolved from the story's complexity score per phase — see
  [Routing policy](routing-policy.md#reasoning-effort-per-phase). An explicit
  profile value still wins.

---

## Anthropic (Claude family)

| Phase | Small | Medium | Large |
|---|---|---|---|
| Preflight | Sonnet | Sonnet | Sonnet |
| Planning | Sonnet | Sonnet | Opus |
| Dev | Sonnet | Sonnet | Opus |
| Review | Sonnet | Sonnet | Sonnet |

**Tier mapping:**
- **cheap:** Haiku — cheap tier for preflight and review (TheForge's own config enables it for both as of v0.13); not dev-capable. Absent from the built-in registry, so it must be declared inline in `models.enabled` with `routing:` and `cost:`
- **mid:** Sonnet — strong all-rounder; handles most stories well
- **strong:** Opus — highest capability; large planning and dev

**Notes:**
- Sonnet is the default for TheForge's CLI transport (`cli: claude, model: sonnet`)
- Opus is expensive; reserve for large stories where Sonnet struggles
- Claude CLI handles session management natively (session ID extraction from stdout)

---

## Google (Gemini family)

Per Google's April 2026 recommendation:

| Phase | Small | Medium | Large |
|---|---|---|---|
| Preflight | `gemini-3.1-pro-preview` + thinking | `gemini-3.1-pro-preview` + thinking | `gemini-3.1-pro-preview` + thinking |
| Planning | not applicable | `gemini-3.1-pro-preview` + thinking | `gemini-3.1-pro-preview` + thinking |
| Dev | `gemini-3-flash-preview` | `gemini-3.1-pro-preview` | `gemini-3.1-pro-preview` |
| Review (audit) | not applicable | `gemini-3.1-pro-preview` | `gemini-3.1-pro-preview` |
| Review (synthesis) | `gemini-3-flash-preview` | `gemini-3-flash-preview` | `gemini-3-flash-preview` |

**Tier mapping:**
- **cheap:** `gemini-3-flash-preview` — lightning-fast for small edits and synthesis tasks
- **strong:** `gemini-3.1-pro-preview` with `thinkingConfig` — architectural reasoning, deep audit, complex planning

**Key points:**
- **Preflight is load-bearing:** Use 3.1 Pro + thinking for all complexities to ensure correct
  complexity routing and avoid false ALREADY_DONE verdicts
- **Dev split:** Flash for small/localized; 3.1 Pro for multi-file plans and test debugging
- **Review synthesis (consensus):** Use Flash even for large stories — merging findings from multiple
  reviewers into structured YAML is mechanical and doesn't need heavyweight reasoning
- **Review audit (deep dive):** Use 3.1 Pro to catch subtle architectural drift and missed acceptance criteria
- `thinkingConfig` (extended thinking) is essential for preflight and planning phases to handle
  complex spec-to-codebase reasoning

---

## DeepSeek

| Phase | Small | Medium | Large |
|---|---|---|---|
| Preflight | `deepseek-reasoner` | `deepseek-reasoner` | `deepseek-reasoner` |
| Planning | not recommended | not recommended | not recommended |
| Dev | not recommended | not recommended | not recommended |
| Review | not recommended | not recommended | not recommended |

**Notes:**
- DeepSeek Reasoner is used for preflight classification — the reasoning
  overhead is justified because preflight drives $20-50 of downstream spend
- Not recommended for dev or review due to tool-use limitations
- Cost: ~$0.30 per preflight call

---

## Pragmatic defaults

If you don't want to think about the full matrix:

| Complexity | Dev | Preflight | Plan | Review (audit) | Review (synthesis) |
|---|---|---|---|---|---|
| Small | `gpt-5.4-mini` | `gemini-3.1-pro` + thinking | `gpt-5.4-mini` | — | `gemini-3-flash` |
| Medium | `gpt-5.4` | `gemini-3.1-pro` + thinking | `gpt-5.4` | `gemini-3.1-pro` | `gemini-3-flash` |
| Large | `gpt-5.4-pro` | `gemini-3.1-pro` + thinking | `gpt-5.4-pro` | `gemini-3.1-pro` | `gemini-3-flash` |

Cross-provider review pools (OpenAI + Google) catch more issues than
single-provider pools. The cost of a second reviewer is small relative to
the cost of a missed P1 causing a dev retry cycle.
