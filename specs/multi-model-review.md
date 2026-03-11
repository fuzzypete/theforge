---
name: "Multi-model review with synthesis"
slug: multi-model-review
file_scope:
  - src/theforge/config.py
  - src/theforge/runner.py
  - src/theforge/coordinator.py
  - src/theforge/task.py
  - tests/test_config.py
  - tests/test_runner.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Multi-Model Review with Synthesis

## Problem

TheForge currently runs a single review agent per cycle. Different models find
fundamentally different issues: adversarial models (Codex) catch crash bugs that
compliance-focused models (Gemini) miss, and vice versa. A single model is an
incomplete reviewer regardless of which model you pick.

## Architecture Overview

Replace the single review agent with a fan-out/synthesis pattern:

```
             ┌── Review Agent A (e.g. Claude Opus) ──┐
code diff ───┤── Review Agent B (e.g. Codex)         ├──→ Synthesis Agent → verdict
             └── Review Agent C (e.g. Gemini)        ┘
```

Each review agent runs independently (blind — they don't see each other's output).
A synthesis agent reads all outputs and produces a single reconciled ReviewResult.

## Requirements

### 1. Config: `review_pool` replaces `review` profile

In `forge.yaml`, the `profiles.review` key becomes `profiles.review_pool` — a list
of model profiles. A single `profiles.synthesis` profile is added for the synthesis
agent. For backward compatibility, a lone `profiles.review` (not a list) is treated
as a pool of one (with no synthesis step needed).

```yaml
profiles:
  dev:
    cli: claude
    model: sonnet
    budget_usd: 2.00

  review_pool:
    - name: opus-reviewer
      cli: claude
      model: opus
      budget_usd: 1.00
      allowed_tools: [Read, Bash, Glob, Grep]
    - name: codex-reviewer
      cli: codex
      model: gpt-4o
      budget_usd: 1.00
      allowed_tools: [Read, Bash, Glob, Grep]

  synthesis:
    cli: claude
    model: opus
    budget_usd: 1.50
    allowed_tools: [Read, Glob, Grep]
```

**Config changes:**
- `ForgeConfig.review_profile` → `ForgeConfig.review_pool: list[ModelProfile]`
- Add `ForgeConfig.synthesis_profile: ModelProfile | None` (None when pool size is 1)
- `load_config()` handles both `review:` (single dict) and `review_pool:` (list)
- Default pool: single Opus reviewer (preserves current behavior)

### 2. Runner: `run_agent_pool()`

Add a new function to `runner.py`:

```python
def run_agent_pool(
    *,
    prompt: str,
    profiles: list[ModelProfile],
    working_dir: Path,
) -> list[AgentResult]:
    """Run multiple agents with the same prompt. Returns results in profile order."""
```

**MVP: run sequentially** (one at a time). Parallel execution is a future optimization.

Each agent in the pool:
- Gets the identical review prompt
- Runs independently (no shared context)
- Returns its own `AgentResult`

### 3. Synthesis prompt template

Add `build_synthesis_prompt()` to `task.py`:

```python
def build_synthesis_prompt(
    task: TaskSpec,
    review_outputs: list[str],  # raw text output from each review agent
    review_model_names: list[str],  # which model produced each output
    spec_content: str,
) -> str:
```

The synthesis prompt instructs the agent to:
1. Read N independent reviews of the same code change
2. Identify **agreements** (high confidence — multiple models concur)
3. Identify **disagreements** (divergence signal — flag for human attention)
4. Identify **unique contributions** (found by one, missed by others)
5. If ANY reviewer found a **reproducible P1**, the synthesized verdict MUST be
   REQUEST_CHANGES regardless of other reviewers' verdicts
6. Produce output in the standard review YAML schema (verdict, summary, findings,
   spec_compliance, test_coverage) — the synthesis result must pass the existing
   schema validator

The synthesis summary should include a divergence note when models disagree
significantly.

### 4. Coordinator: wire in the pool + synthesis

In `coordinator.py`, the REVIEW phase changes from:

```python
review_result = run_agent(prompt=review_prompt, profile=config.review_profile, ...)
```

To:

```python
pool_results = run_agent_pool(
    prompt=review_prompt,
    profiles=config.review_pool,
    working_dir=workspace_path,
)
# Track all pool results for cost
for r in pool_results:
    state.review_agent_results.append(r)

if len(pool_results) == 1:
    # Single reviewer — no synthesis needed
    synthesis_output = pool_results[0].output
else:
    # Multi-model — synthesize
    synthesis_prompt = build_synthesis_prompt(
        task,
        review_outputs=[r.output for r in pool_results],
        review_model_names=[p.model for p in config.review_pool],
        spec_content=spec_content,
    )
    synthesis_result = run_agent(
        prompt=synthesis_prompt,
        profile=config.synthesis_profile,
        working_dir=workspace_path,
    )
    state.review_agent_results.append(synthesis_result)
    synthesis_output = synthesis_result.output

parsed_review = parse_review_output(synthesis_output)
```

**Budget enforcement:** each pool agent's cost is tracked via
`state.review_agent_results`. The synthesis agent's cost is also tracked there.
The existing `total_review_cost` property already sums all entries. Budget check
against `synthesis_profile.budget_usd` applies only to the synthesis agent itself.
Each pool agent's cost is checked against its own profile's budget.

### 5. Audit log update

The audit log's `reviews` section should include which models were in the pool:

```yaml
reviews:
  - cycle: 1
    pool_models: ["opus", "gpt-4o"]
    synthesized: true
    verdict: REQUEST_CHANGES
    summary: "..."
    p1_count: 2
    p2_count: 1
```

### 6. Tests

- `test_config.py`: loading `review_pool` list + backward compat with `review` dict
- `test_runner.py`: `run_agent_pool()` runs N agents sequentially, returns N results
- `test_coordinator.py`: pool of 2 reviews → synthesis → APPROVE/REQUEST_CHANGES
- `test_coordinator.py`: pool of 1 → skips synthesis (backward compat)
- `test_coordinator.py`: pool budget enforcement per-agent

## Non-goals

- Parallel agent execution (future optimization — run sequentially for MVP)
- Synthesis for the dev stage (only review stage for now)
- Human-in-the-loop question rounds within synthesis (future feature)
- Codex/Gemini runner implementations (stub them; only claude runner works today)

## Open questions for human review

1. Should the synthesis agent run in the worktree (read access to code) or only
   receive the review outputs as text? Running in worktree lets it verify claims.
2. Should we cap the pool at a maximum size in config validation?
3. When a pool agent fails (exit code != 0), should we still synthesize the
   remaining results, or escalate immediately?
