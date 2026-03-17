# Implementation Plan: Agent Review of Plan Before Dev

## Summary

Replace the HITL plan review gate with an automated agent review. A lightweight agent reads the story + generated plan, produces APPROVE or REJECT. On REJECT, the plan regenerates once and is re-reviewed. Two REJECTs → escalate. The existing PLAN_REVIEW phase is reused — the agent replaces the human, not the phase.

## Implementation Order

### 1. Add `PlanAgentReviewConfig` to `config.py`

New config section alongside the existing `PlanReviewConfig`:

```yaml
plan_agent_review:
  enabled: false
  profile: sonnet   # references a named profile from the profiles section
  budget_usd: 0.50
  timeout: 300
```

The `profile` field references a named profile from the `profiles` section in forge.yaml (e.g. `sonnet`, `opus`, `codex`), which already specifies both CLI and model. This avoids ambiguity — `claude` is a CLI provider, not a model. `run_agent()` requires a full `ModelProfile` with both.

Parsed into a new `PlanAgentReviewConfig` dataclass. When `plan_agent_review.enabled` is true, it takes precedence over `plan_review.enabled` — they're mutually exclusive (agent review replaces human review, not stacks on top).

### 2. Add `build_plan_review_prompt()` to `task.py`

New prompt builder. Inputs: story content, plan content, file_scope contents (current source of scoped files), preflight output. Output: a prompt asking the agent to produce a structured verdict:

```yaml
verdict: APPROVE | REJECT
findings:
  - severity: P1
    description: "..."
    suggestion: "..."
```

The prompt instructs the agent to evaluate:
- Does the plan address all acceptance criteria in the story?
- Are there technical errors (wrong APIs, hallucinated functions, blast radius gaps)?
- Is the implementation order sound?
- Do the proposed function signatures and module references match the actual codebase?

The agent needs real code context to catch hallucinated APIs and blast radius issues — story + plan alone is not enough. Include the same `file_scope` contents that `build_dev_prompt` uses, plus preflight output if available.

### 3. Add plan review verdict parser to `review.py`

Simple parser for the plan review YAML output. Reuse the existing YAML extraction logic (`_extract_yaml_block`). Returns a dataclass:

```python
@dataclass
class PlanReviewResult:
    verdict: str  # "APPROVE" or "REJECT"
    findings: list[PlanReviewFinding]
```

Validation: verdict must be APPROVE or REJECT. REJECT without findings is an error (same cross-validation principle as code review).

### 4. Replace PLAN_REVIEW phase logic in `coordinator.py`

The existing PLAN_REVIEW block (lines ~897-992) currently dispatches to interactive or remote human review. Add a third path at the top:

```
if config.plan_agent_review.enabled:
    → run agent plan review (new code)
elif config.plan_review.enabled:
    → run human plan review (existing code, unchanged)
else:
    → skip (existing code, unchanged)
```

The agent review path:
1. Call `run_agent()` with the plan review prompt and the configured model profile
2. Parse the verdict
3. On APPROVE → set `state.plan_review_decision = "approve"`, continue to DEV
4. On REJECT → if not already regenerated, regenerate plan and loop back
5. On second REJECT → escalate with rejection findings
6. Track cost in a new `state.plan_review_results: list[AgentResult]` — do NOT append to `state.plan_results` which is used for plan generation invocations, `total_plan_cost`, regeneration accounting, and tests that assume its length matches the number of plan generations

### 5. Add state tracking fields to `coord_state.py`

- `plan_agent_review_findings: str | None = None` — rejection findings for audit/logging and for feeding back into plan regeneration
- `plan_review_results: list[AgentResult] = field(default_factory=list)` — separate from `plan_results` to avoid corrupting plan generation cost tracking
- Reuse existing `plan_review_decision`, `plan_regenerated`, `plan_review_waited_seconds` — these are phase-level, not human-specific

### 6. Update audit log in `coord_audit.py`

The existing `plan_review` section already captures decision and regeneration. Add:
- `reviewer: "agent"` vs `"human"` to distinguish
- `findings` from plan review result (when REJECT)
- `cost_usd` for the agent invocation

### 7. Skip plan review when `--plan` is injected

Already handled — line 757-763 skips PLAN_REVIEW when plan is injected. No change needed, but verify the agent review path respects this.

### 8. Update `forge.yaml` for dogfooding

```yaml
plan_agent_review:
  enabled: true
  profile: sonnet   # fast, cheap — plan review doesn't need opus
  budget_usd: 0.50
  timeout: 300

plan_review:
  enabled: false  # human gate disabled
```

### 9. Tests

In `tests/test_coordinator.py`:

- **test_plan_agent_review_approve** — agent returns APPROVE, pipeline continues to DEV
- **test_plan_agent_review_reject_then_approve** — first review REJECT, plan regenerated, second review APPROVE, pipeline continues
- **test_plan_agent_review_double_reject_escalates** — two REJECTs, run escalates with findings
- **test_plan_agent_review_disabled_by_default** — config without plan_agent_review section, PLAN_REVIEW phase is skipped
- **test_plan_agent_review_skipped_on_plan_injection** — `--plan` flag skips agent review
- **test_plan_agent_review_parse_failure** — agent produces garbage, treated as REJECT
- **test_plan_agent_review_cost_in_audit** — plan review cost appears in audit log

In `tests/test_task.py`:
- **test_build_plan_review_prompt** — prompt contains story and plan content

In `tests/test_review.py`:
- **test_parse_plan_review_approve** — valid APPROVE YAML
- **test_parse_plan_review_reject_with_findings** — valid REJECT with findings
- **test_parse_plan_review_reject_no_findings_error** — REJECT without findings fails validation

## Files to Modify

| File | Change |
|------|--------|
| `src/theforge/config.py` | Add `PlanAgentReviewConfig`, parse from forge.yaml |
| `src/theforge/task.py` | Add `build_plan_review_prompt()` |
| `src/theforge/review.py` | Add `PlanReviewResult`, parser |
| `src/theforge/coordinator.py` | Agent review path in PLAN_REVIEW block |
| `src/theforge/coord_state.py` | Add `plan_agent_review_findings` and `plan_review_results` fields |
| `src/theforge/coord_audit.py` | Add reviewer type and findings to plan_review audit |
| `forge.yaml` | Add `plan_agent_review` section |
| `tests/test_coordinator.py` | 7 new tests |
| `tests/test_task.py` | 1 new test |
| `tests/test_review.py` | 3 new tests |

## Risks

- **Parse reliability** — plan review agent may not produce clean YAML. Mitigation: reuse the existing `_extract_yaml_block` logic with retry (same as code review parsing). Treat unparseable output as REJECT.
- **Config collision** — both `plan_review.enabled` and `plan_agent_review.enabled` set to true. Mitigation: agent review takes precedence, log a warning.
- **Regeneration uses same prompt** — if the plan was bad because the story was bad, regeneration won't help. Mitigation: on REJECT, the rejection findings are appended to the plan prompt so the plan agent knows what to fix. This is the same pattern as dev retry with review findings. **This must be implemented explicitly in step 4**: when regenerating after REJECT, pass `state.plan_agent_review_findings` to `build_plan_prompt()` (or append to the existing prompt) so the plan agent sees what was wrong. Add a test for this: `test_plan_regen_receives_rejection_findings`.
