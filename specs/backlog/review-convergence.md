---
name: "Review convergence — cycle-aware prompts and deterministic exit criteria"
slug: review-convergence
pytest_target: tests/
---

# Review convergence — cycle-aware prompts and deterministic exit criteria

## Problem

When code review uses a multi-model pool (4 independent reviewers), each review cycle
surfaces *new* P1 findings that weren't caught in prior cycles. This is because:

1. **Reviewers are blind to previous cycles** — `build_review_prompt()` has no
   `cycle_history` parameter. Each reviewer does a full fresh review with zero
   knowledge of what was previously raised or fixed. The dev prompt already has cycle
   history wired in (`build_dev_prompt()` and `build_fix_prompt()` both render
   `CycleHistory`), but the review side was never connected.

2. **Exit criteria conflate "newly discovered" with "newly introduced"** — a single P1
   from any reviewer triggers REQUEST_CHANGES regardless of whether it's an unresolved
   prior finding, a regression from a fix, or a net-new latent issue that was always
   there but missed. This creates churn: each cycle the code gets better but reviewers
   find new things, so the sprint never converges.

3. **CycleHistory is too lossy** — it stores only cycle number, verdict, summary, and
   truncated P1 descriptions. Not enough for deterministic finding classification
   (matching findings across cycles, correlating with changed files).

4. **Parse retry is stateless** — when a reviewer's output fails schema validation, the
   corrective prompt goes through `run_agent()` which starts a fresh session. The agent
   re-reviews from scratch instead of just reformatting its existing findings.

## Solution

Four coordinated changes, all in coordinator/prompt layer — no LLM in control flow.

### 1. Wire cycle_history into review prompts

Add `cycle_history` parameter to `build_review_prompt()` in `task.py`. For cycle 2+,
render a tri-part review framing:

1. **Verify fixes**: Prior P1 findings listed explicitly — reviewer confirms fixed or
   still present
2. **Scan regressions**: Emphasis on files changed in the latest dev iteration
3. **Additional findings**: Reviewer may report new concerns, but P1 only if they are
   direct regressions from the fix OR critical and independently evidenced

For cycle 1, prompt is unchanged (full independent review).

### 2. Richer finding tracking in coordinator state

Extend `coord_state.py` with a `FindingRecord` dataclass:

- `finding_id`: stable identifier (hash of severity + file + normalized description
  tokens)
- `cycle_first_seen`: cycle number when first raised
- `cycle_last_seen`: cycle number when last raised
- `file`: file path
- `line`: line number (nullable)
- `severity`: P1 or P2
- `description`: canonicalized description
- `reporter`: reviewer name that raised it
- `disposition`: enum — `unresolved | fixed | regression | net_new |
  corroborated_new | downgraded`

Store a `finding_registry: list[FindingRecord]` on `CoordinatorState`. Update it each
cycle after parsing review results.

### 3. Deterministic finding classification

In the coordinator (Python, not LLM), after parsing cycle 2+ review results:

1. **Fingerprint** each new finding: severity + file + normalized description tokens
2. **Match against prior findings** using fingerprint similarity (file match + token
   overlap threshold)
3. **Correlate with changed files**: `git diff` from previous dev iteration shows what
   was touched
   - Finding in/near changed code -> candidate regression
   - Finding far from changed surface -> candidate latent/net-new
4. **Check corroboration**: if 2+ reviewers independently raise substantially the same
   net-new finding in the same cycle, it's corroborated
5. **Assign disposition** deterministically:
   - Matches prior unresolved finding -> `unresolved`
   - In changed files, not previously raised -> `regression`
   - Raised by 2+ reviewers, not previously raised -> `corroborated_new`
   - Single reviewer, not in changed files, not previously raised -> `net_new`

### 4. Smarter exit criteria

Replace the current "any P1 -> REQUEST_CHANGES" with:

- `unresolved` P1 -> **hard block** (raised before, not fixed)
- `regression` P1 -> **hard block** (fix introduced new problem)
- `corroborated_new` P1 -> **hard block** (2+ reviewers agree it's real)
- `net_new` P1 (single reviewer, latent) -> **record but don't block** — file as
  issue, include in audit, feed to next sprint

The coordinator owns this decision, not the synthesis model. Synthesis still
summarizes, but the coordinator assigns blocking class.

### 5. Parse retry with context

For the corrective prompt retry when a reviewer's output fails schema validation:

- **CLI agents**: pass `session_id` to `run_agent()` so the retry resumes the existing
  session
- **API agents**: include the reviewer's original raw output in the corrective prompt
  so the agent can reformat without re-reviewing

The corrective prompt becomes: "Your previous output (reproduced below) had schema
errors: {errors}. Reformat it as valid YAML. Do NOT re-review the code."

Then include the original output.

## Acceptance criteria

- [ ] `build_review_prompt()` accepts `cycle_history` parameter
- [ ] Cycle 2+ review prompts include previous cycle findings and tri-part framing
- [ ] Cycle 1 review prompts unchanged (full independent review)
- [ ] `FindingRecord` dataclass in `coord_state.py` with all specified fields
- [ ] `CoordinatorState` has `finding_registry` field
- [ ] Finding registry updated after each review cycle
- [ ] Deterministic fingerprint matching: file + normalized description tokens
- [ ] Changed-file correlation via git diff from previous dev iteration
- [ ] Corroboration detection: 2+ reviewers raising same net-new finding
- [ ] Disposition assignment is pure Python, no LLM
- [ ] Exit criteria: unresolved/regression/corroborated_new P1s block; single-reviewer net_new P1s don't block
- [ ] Net-new non-blocking P1s recorded in audit trail with explicit labeling
- [ ] Parse retry for CLI agents passes session_id for session continuity
- [ ] Parse retry for API agents includes original output in corrective prompt
- [ ] Corrective prompt says "reformat, do NOT re-review"
- [ ] All existing tests pass
- [ ] New tests for finding fingerprinting, classification logic, and exit criteria
