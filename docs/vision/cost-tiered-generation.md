# Vision: Cost-Tiered Generation — Local & Cheap Models for DEV

Captured 2026-03-16 from analysis of phase-specific model economics.

## Context

TheForge's smart config already assigns models by cost_rank and capability,
and the coordinator already distinguishes first-pass DEV from repair-pass DEV
via `retry_reason` and `build_fix_prompt()`. The missing piece: the DEV slot
today is always a subscription-tier CLI model (Claude Sonnet, Codex, Gemini).

For mechanical implementation work — especially when a strong PLAN already
exists — a local or cheap API model could do the job at near-zero cost,
reserving expensive models for PLAN, REVIEW, and late-cycle repair.

## The Economic Argument

The cost structure of a typical forge run:

| Phase        | Token volume | Reasoning demand | Cost sensitivity |
|--------------|-------------|------------------|-----------------|
| PREFLIGHT    | Low         | Classification   | Low             |
| PLAN         | Medium      | High (architecture) | Worth paying for |
| DEV pass 1   | High        | Medium (follow the plan) | **Highest leverage for savings** |
| REVIEW       | Medium      | High (critical analysis) | Worth paying for |
| DEV repair   | Low-Medium  | Medium (surgical fixes)  | Moderate        |

DEV pass 1 is the highest-volume, lowest-reasoning-demand phase — the best
candidate for a cheap model. But only if the plan is strong enough to
constrain the implementation space.

## Prerequisite: Reviewed Plans

A cheap generator is only as good as the plan it follows. Without plan
maturity, a weak model flails and burns review cycles — costing more than
just using Sonnet.

**Required before trialing cheap DEV models:**

1. **Structured plan output** — evolve `build_plan_prompt()` toward a
   step-by-step work order format rather than freeform markdown:
   ```yaml
   steps:
     - file: src/theforge/runner.py
       action: add function _run_aider
       details: "Follow _run_codex pattern, subprocess with --message flag"
       spec_requirement: "Support local model execution"
     - file: src/theforge/config.py
       action: add ModelInfo entry
       details: "aider/deepseek-coder-v2, tier=fast, cost_rank=0"
       spec_requirement: "Registry entry for local models"
   ```
   Each step maps to a spec requirement. The coordinator can validate
   coverage mechanically (every spec requirement referenced at least once).

2. **Plan review gate** — `PLAN -> PLAN_REVIEW(pool) -> HUMAN_REVIEW -> DEV`
   as described in `docs/vision/agent-intelligence.md` section 6. A reviewed
   plan is the safety net that makes a weaker DEV model viable.

3. **Plan validation** — lightweight structural check: did the plan YAML
   parse? Does every spec requirement have at least one step? Are file
   paths valid? This can be mechanical (no LLM needed).

## CLI Adapter Candidates

Research into CLI tools that support local/cheap models and can be invoked
as subprocesses (the same pattern as `_run_claude`, `_run_codex`,
`_run_gemini`):

### Tier 1: Forge-Ready

**Aider** (`aider-chat`) — Strongest candidate for first adapter.
- Install: `pip install aider-chat`
- Headless: `aider --message "prompt" --yes --auto-commits --model ollama_chat/deepseek-coder-v2`
- Models: Ollama local, DeepSeek API, Mistral/Codestral, OpenRouter,
  Anthropic, OpenAI, Google — practically everything
- File editing: native, auto-commits to git
- Output: plain text stdout, exit code for success/failure
- Adapter complexity: LOW — clean subprocess, ~40 lines following Codex pattern
- Key advantage: handles file editing, git commits, and tool use natively.
  No need to design a patch-application layer.

**Cline CLI 2.0** — Best structured output.
- Install: `npm install -g cline`
- Headless: `cline -y --json "prompt"` or stdin pipe
- Models: Anthropic, OpenAI, Google, Ollama, DeepSeek, OpenRouter
- Output: `--json` streams structured JSON — ideal for coordinator parsing
- Adapter complexity: LOW

**OpenCode** — Cleanest subprocess interface.
- Install: single Go binary from opencode.ai
- Headless: `opencode run --model ollama/qwen3 "prompt"`
- Models: 75+ providers including Ollama, DeepSeek, Mistral
- Output: text stdout, `--quiet` for script-friendly mode
- Adapter complexity: LOW

### Tier 2: Viable with Caveats

**Goose** (Block) — Recipe system adds power but complexity.
- Headless: `GOOSE_MODE=auto goose run --recipe task.yaml`
- Models: Ollama, Anthropic, OpenAI, Google
- Adapter complexity: MEDIUM (recipe YAML layer)

**Continue CLI** (`cn`) — Clean but newer.
- Headless: `cn -p "prompt" --config config.yaml`
- Models: Ollama, all major APIs
- Adapter complexity: LOW

**Mistral Vibe CLI** — Useful if targeting Devstral/Codestral specifically.
- Headless: `vibe --prompt "prompt" --max-turns 20 --enabled-tools "bash*,file*"`
- Models: Mistral ecosystem
- Adapter complexity: LOW

### Not Suitable

- **Open Interpreter**: stability issues in headless mode with local models
- **Plandex**: client-server Docker architecture, too heavy
- **Cursor CLI**: requires real TTY, subprocess hangs without tmux

## Target Model Assignments

With a local/cheap adapter available, smart config would naturally produce:

| Phase        | Model                          | Why                          |
|--------------|--------------------------------|------------------------------|
| PREFLIGHT    | cheapest fast-tier             | Classification task          |
| PLAN         | Claude Opus (or strongest)     | Architectural reasoning      |
| PLAN_REVIEW  | Review pool (existing)         | Critical analysis            |
| DEV pass 1   | **Local/cheap via aider**      | Follow the plan, high volume |
| VALIDATE     | Gate command (no LLM)          | Mechanical                   |
| REVIEW       | Review pool (existing)         | Critical analysis            |
| DEV repair   | Codex or Sonnet                | Surgical fixes from findings |

The key insight: DEV pass 1 and DEV repair are different jobs. Pass 1 is
volume implementation from a plan. Repair is surgical compliance with
specific findings. The prompt routing (`build_dev_prompt` vs `build_fix_prompt`)
already handles this; the model assignment should reflect it too.

## Implementation Path

### Phase 0: Plan Maturity (prerequisite)
- Structured plan output format
- Plan review gate (per agent-intelligence.md vision)
- Mechanical plan validation

### Phase 1: Aider Adapter
- `_run_aider()` in runner.py (~40 lines, follows Codex pattern)
- `ModelInfo` registry entries for aider-backed models
- Config: `cli: aider`, `model: ollama_chat/deepseek-coder-v2`
- Smart config slots aider models as cheapest dev-capable (`cost_rank=0`)

### Phase 2: Trial
- Pick a small-complexity spec with a strong plan
- Run with aider/local for DEV, Claude for everything else
- Compare: cycle count, total cost, time-to-approve
- Measure: does the local model follow the plan? Does it commit cleanly?
  Does it run the gate?

### Phase 3: Telemetry
- Track phase-specific model performance over time
- Per-model metrics: cycle count to approval, review churn rate, cost
- Answer: "for this kind of task, which planner + generator + reviewer
  combination yields the lowest total cycles to approval?"
- Feed back into smart config model selection

## What This Is NOT

This is not about replacing strong models. It's about **cost allocation
across phases**. The expensive models earn their cost in PLAN and REVIEW
where reasoning quality determines outcomes. DEV pass 1, when constrained
by a strong plan, is closer to transcription than reasoning — and that's
where cheap/local models can deliver.

## Relationship to Existing Architecture

- **Smart config** already assigns by cost_rank — local models slot in
  naturally as `cost_rank=0`
- **Prompt routing** already distinguishes first-pass from repair via
  `retry_reason` — no coordinator changes needed
- **Session resume** is per-profile — switching models between passes is
  already supported (repair model gets its own session)
- **Dev model escalation** already handles "this model can't do it" —
  persistent P1 detection escalates to a stronger model automatically
- **Complexity adaptation** already adjusts strategy by task size —
  small tasks might skip the cheap model entirely (not worth the overhead)
