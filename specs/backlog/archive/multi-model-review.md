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

**Note on performance:** Running N+1 sequential agents (N reviewers + 1 synthesis)
significantly increases the REVIEW phase duration (e.g., 3 agents at 5m each +
synthesis at 5m = 20m). This is an acceptable trade-off for the MVP — quality over
speed. Parallel execution is a future optimization (see Non-goals).

## Requirements

### 1. Config: `review_pool` alongside backward-compatible `review`

In `forge.yaml`, the `profiles.review` key can be replaced by `profiles.review_pool`
— a list of model profiles. A single `profiles.synthesis` profile is added for the
synthesis agent.

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

**Config precedence rules (deterministic, no ambiguity):**

1. If `profiles.review_pool` is present, it is used. `profiles.review` is ignored
   even if also present.
2. If only `profiles.review` is present (single dict), it is wrapped into a pool
   of one. No synthesis step is needed.
3. If neither is present, the default pool is a single Opus reviewer (current
   behavior preserved).

**Validation rules (`load_config()` must enforce):**

- `review_pool` must contain **at least 1** profile. Empty list → `ValueError`.
- Each pool entry must have a `name` field. Duplicate names → `ValueError`.
- Each pool entry's `cli` must be a supported runner (`"claude"` for MVP).
  Unsupported CLI → `ValueError` at config load, not silent runtime failure.
- If pool size > 1, `profiles.synthesis` is **required**. Missing → `ValueError`.
- If pool size == 1, `profiles.synthesis` is ignored (no synthesis step).

**Python API changes:**

- `ForgeConfig.review_pool: list[ModelProfile]` — the pool of reviewers.
- `ForgeConfig.synthesis_profile: ModelProfile | None` — None when pool size is 1.
- `ForgeConfig.review_profile: ModelProfile` — **kept as a read-only property**
  returning `review_pool[0]`. This preserves backward compatibility for CLI
  (`cli.py:138-142,195`) and coordinator references. No existing code breaks.
- Default: `review_pool = [DEFAULT_REVIEW_PROFILE]`, `synthesis_profile = None`.

**`_parse_profile()` update:**

Pool entries use the same `_parse_profile()` function but pass the entry's `name`
field instead of the role string. The `name` field is how profiles are identified
for attribution and budget tracking.

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

**MVP: run sequentially** (one at a time). Parallel execution is a future
optimization.

Each agent in the pool:
- Gets the identical review prompt
- Runs independently (no shared context)
- Returns its own `AgentResult`

Results are returned in the same order as the input profiles list. This ordering
guarantee must be tested.

**`AgentResult` change — add profile identifier:**

```python
@dataclass(frozen=True)
class AgentResult:
    success: bool
    output: str
    session_id: str | None
    cost_usd: float
    exit_code: int
    raw: dict[str, Any]
    profile_name: str = ""  # NEW: identifies which profile produced this result
```

`run_agent()` sets `profile_name=profile.name`. `run_agent_pool()` relies on this
for per-profile budget tracking in the coordinator. Existing callers are unaffected
(default empty string).

### 3. Synthesis prompt template

Add `build_synthesis_prompt()` to `task.py`:

```python
def build_synthesis_prompt(
    task: TaskSpec,
    review_outputs: list[str],     # raw text output from each review agent
    review_names: list[str],       # profile name (not model) for each output
    spec_content: str,
) -> str:
```

**Attribution uses profile `name`, not `model`**, because multiple profiles may
use the same model string but different configurations.

**Prompt framing — delimited sections per reviewer:**

The synthesis prompt must frame each review output in clearly delimited sections
to prevent confusion or injection from raw review text:

```
## Review from "opus-reviewer"
--- BEGIN REVIEW OUTPUT ---
{review_outputs[0]}
--- END REVIEW OUTPUT ---

## Review from "codex-reviewer"
--- BEGIN REVIEW OUTPUT ---
{review_outputs[1]}
--- END REVIEW OUTPUT ---
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

### 4. Pool failure policy

**This is not an open question. The policy is defined here.**

When a pool agent fails (timeout, crash, unsupported CLI at runtime, exit code
!= 0), the coordinator applies these rules:

1. **Filter**: Only successful results (`result.success == True`) are passed to
   the synthesis agent. Failed results are logged but excluded.

2. **Threshold**: If the number of successful results is **< 1** (i.e., all
   reviewers failed), skip synthesis and transition to ESCALATE with error
   message listing which agents failed and why.

3. **Degraded synthesis**: If some agents succeeded and some failed, synthesis
   proceeds with the successful outputs. The synthesis prompt includes a note:
   `"Note: {N} of {M} reviewers failed and their outputs are excluded."`

4. **Single-reviewer degradation**: If the pool has 2+ profiles but only 1
   succeeds, that single output is used directly (no synthesis step), same as
   the pool-of-one path.

### 5. Coordinator: wire in the pool + synthesis

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

# Track all pool results for cost (profile_name on each AgentResult
# enables per-profile budget enforcement below)
for r in pool_results:
    state.review_agent_results.append(r)

# Filter to successful results only
successful = [r for r in pool_results if r.success]
failed = [r for r in pool_results if not r.success]

for f in failed:
    _log(f"Pool reviewer failed: {f.profile_name} (exit={f.exit_code})")

if len(successful) == 0:
    # All reviewers failed — escalate
    state.phase = Phase.ESCALATE
    state.error = f"All {len(pool_results)} review agents failed"
    return CoordinatorResult(...)

if len(successful) == 1 or len(config.review_pool) == 1:
    # Single successful reviewer — no synthesis needed
    synthesis_output = successful[0].output
else:
    # Multi-model — synthesize successful outputs
    synthesis_prompt = build_synthesis_prompt(
        task,
        review_outputs=[r.output for r in successful],
        review_names=[r.profile_name for r in successful],
        spec_content=spec_content,
    )
    synthesis_result = run_agent(
        prompt=synthesis_prompt,
        profile=config.synthesis_profile,
        working_dir=workspace_path,
    )
    synthesis_result = AgentResult(  # tag with profile name
        ...synthesis_result, profile_name="synthesis"
    )
    state.review_agent_results.append(synthesis_result)
    synthesis_output = synthesis_result.output

parsed_review = parse_review_output(synthesis_output)
```

**Synthesis failure handling:** If the synthesis agent itself fails (`success ==
False`) or produces unparseable output (schema errors), the coordinator treats
this as ESCALATE — not a fallback to majority-rules. The synthesis agent is the
authority; if it fails, a human must intervene. This is the same behavior as a
single reviewer producing garbage today.

### 6. Budget enforcement

**Cost model redesign — per-profile tracking:**

The existing `state.review_agent_results` list now contains `AgentResult` objects
tagged with `profile_name`. Budget enforcement uses per-profile accounting:

```python
# Per-profile cumulative cost check (in coordinator, after each pool run)
for profile in config.review_pool:
    profile_cost = sum(
        r.cost_usd for r in state.review_agent_results
        if r.profile_name == profile.name
    )
    if profile_cost > profile.budget_usd:
        # Escalate — this profile has exceeded its budget
        state.phase = Phase.ESCALATE
        state.error = f"Review budget exceeded for {profile.name}: ..."
        return CoordinatorResult(...)

# Synthesis agent budget check (separate from pool)
if config.synthesis_profile:
    synth_cost = sum(
        r.cost_usd for r in state.review_agent_results
        if r.profile_name == "synthesis"
    )
    if synth_cost > config.synthesis_profile.budget_usd:
        state.phase = Phase.ESCALATE
        state.error = "Synthesis budget exceeded: ..."
        return CoordinatorResult(...)
```

**`total_review_cost` property remains as-is** — it sums all entries in
`review_agent_results` for reporting/audit purposes. It is NOT used for budget
enforcement (enforcement is per-profile).

### 7. Audit log update

The audit log's `reviews` section includes pool metadata per cycle. The coordinator
must store this metadata during the review phase (add to `CoordinatorState`):

```python
@dataclass
class ReviewCycleMetadata:
    """Per-cycle metadata for audit logging."""
    pool_models: list[str]     # profile names of all pool agents
    successful: list[str]      # profile names that succeeded
    failed: list[str]          # profile names that failed
    synthesized: bool          # whether synthesis ran
```

Audit output:

```yaml
reviews:
  - cycle: 1
    pool_models: ["opus-reviewer", "codex-reviewer"]
    successful: ["opus-reviewer", "codex-reviewer"]
    failed: []
    synthesized: true
    verdict: REQUEST_CHANGES
    summary: "..."
    p1_count: 2
    p2_count: 1
```

### 8. Tests

**Config tests (`test_config.py`):**
- Loading `review_pool` list produces correct `review_pool` and `synthesis_profile`
- Backward compat: `review` dict → pool of one, `synthesis_profile` is None
- `review_profile` property returns `review_pool[0]`
- Precedence: both `review` and `review_pool` present → `review_pool` wins
- Validation: empty `review_pool` → `ValueError`
- Validation: duplicate pool names → `ValueError`
- Validation: unsupported CLI in pool → `ValueError`
- Validation: pool size > 1 with missing `synthesis` → `ValueError`

**Runner tests (`test_runner.py`):**
- `run_agent_pool()` runs N agents sequentially, returns N results
- Results are in profile order (assert ordering matches input)
- `AgentResult.profile_name` is set correctly by `run_agent()` and `run_agent_pool()`

**Coordinator tests (`test_coordinator.py`):**
- Pool of 2 reviews → synthesis → APPROVE
- Pool of 2 reviews → synthesis → REQUEST_CHANGES
- Pool of 1 → skips synthesis (backward compat)
- Mixed success/failure: 1 of 2 succeeds → uses single output, no synthesis
- All reviewers fail → ESCALATE
- Synthesis agent failure → ESCALATE
- Per-profile budget enforcement: one profile over budget → ESCALATE
- Synthesis budget enforcement: synthesis over budget → ESCALATE
- Audit log contains `pool_models`, `synthesized`, `successful`, `failed` fields

## Non-goals

- Parallel agent execution (future optimization — run sequentially for MVP)
- Synthesis for the dev stage (only review stage for now)
- Human-in-the-loop question rounds within synthesis (future feature)
- Codex/Gemini runner implementations (stub them; only claude runner works today;
  unsupported CLIs are rejected at config load)
- Fallback strategies when synthesis fails (majority-rules, etc.) — synthesis
  failure is an ESCALATE, period
