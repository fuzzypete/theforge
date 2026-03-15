---
name: "Reviewer role specialization"
slug: reviewer-role-specialization
file_scope:
  - src/theforge/task.py
  - src/theforge/config.py
  - src/theforge/coordinator.py
  - src/theforge/runner.py
  - tests/test_task.py
  - tests/test_coordinator.py
  - tests/test_runner.py
pytest_target: tests/
---

# Reviewer Role Specialization

## Problem

All reviewers in the pool receive the identical prompt from `build_review_prompt()`.
Three models answering the same question from the same angle produce convergent
findings. This wastes the diversity benefit of multi-model review — especially
Gemini, which is fast but produces "same take, different words" when given the
same generic review lens as Claude and Codex.

## Context

`build_review_prompt()` in `task.py` builds one prompt. The coordinator passes
that single prompt to `run_agent_pool()`. Each reviewer gets the same "verify
spec compliance, correctness, test coverage" instruction set.

The synthesis prompt (`build_synthesis_prompt()`) already names each reviewer
by name in the output it receives. The infrastructure for per-reviewer identity
exists — only the review prompt itself is uniform.

## Solution

### 1. Add `review_role` to `ModelProfile` (config.py)

Add an optional `review_role: str | None = None` field to `ModelProfile`.

Default roles when `review_role` is not set in `forge.yaml`:
- If no role is configured, fall back to the current generic prompt (backward
  compatible).

Example `forge.yaml`:
```yaml
review_pool:
  - name: sonnet
    cli: claude
    model: sonnet
    review_role: correctness
    ...
  - name: codex
    cli: codex
    model: gpt-5.4
    review_role: patterns
    ...
  - name: gemini
    cli: gemini
    model: gemini-2.5-pro
    review_role: edge-cases
    ...
```

### 2. Add role-specific review lenses to `build_review_prompt()` (task.py)

The `build_review_prompt()` function gains a `review_role: str | None = None`
parameter. When set, the "Your Role" section is replaced with a role-specific
lens. Three built-in roles:

**`correctness`** (default / fallback):
- Spec compliance verification
- Logic and correctness bugs
- Data integrity risks
- Security issues

**`patterns`**:
- API usage patterns and idiom violations
- Error handling completeness
- Test coverage gaps and missing edge-case tests
- Code organization and interface design

**`edge-cases`**:
- Boundary conditions and off-by-one errors
- Race conditions and concurrency hazards
- State that survives when it shouldn't (cleanup paths)
- Failure modes under unexpected input or timing

All roles share the same output format (YAML), severity definitions, and rules.
Only the "Your Role" instruction block and the lens emphasis differ.

Unknown role values fall back to the generic prompt (no error).

### 3. Build per-reviewer prompts in coordinator (coordinator.py)

Instead of building one `review_prompt` and passing it to `run_agent_pool()`,
build a list of `(prompt, profile)` pairs. The coordinator calls
`build_review_prompt()` once per profile, passing `review_role=profile.review_role`.

`run_agent_pool()` must accept either a single prompt (current behavior) or a
list of prompts (one per profile). When a list is passed, each agent gets its
own prompt.

### 4. Wire `reasoning_effort` to CLI flags in runner.py

`ModelProfile` already has `reasoning_effort: str | None = None` but it is
unused. Wire it through in `runner.py` when spawning reviewer subprocesses.

**Gemini CLI** — pass as `--config` flags:

| `reasoning_effort` | Gemini flags |
|--------------------|-------------|
| `"low"` | `--config modelConfigs.default.thinkingConfig.thinkingBudget=1024 --config modelConfigs.default.thinkingConfig.thinkingLevel=LOW` |
| `"medium"` | `--config modelConfigs.default.thinkingConfig.thinkingBudget=4096 --config modelConfigs.default.thinkingConfig.thinkingLevel=MEDIUM` |
| `"high"` | `--config modelConfigs.default.thinkingConfig.thinkingBudget=8192 --config modelConfigs.default.thinkingConfig.thinkingLevel=HIGH` |
| `None` | No flags added (CLI default) |

**Codex CLI** — pass as `--reasoning-effort`:

| `reasoning_effort` | Codex flag |
|--------------------|-----------|
| `"low"` | `--reasoning-effort low` |
| `"medium"` | `--reasoning-effort medium` |
| `"high"` | `--reasoning-effort high` |
| `None` | No flag added |

**Claude CLI** — no flag needed; extended thinking is configured separately
and not controlled per-invocation via CLI flag. Ignore `reasoning_effort` for
claude profiles.

These flags are injected into the subprocess argv list before the prompt
argument. The runner already constructs argv per-CLI-type — add the effort
flags to the gemini and codex branches.

Example `forge.yaml` with both role and effort configured:
```yaml
review_pool:
  - name: sonnet
    cli: claude
    model: sonnet
    review_role: correctness
  - name: codex
    cli: codex
    model: gpt-5.4
    review_role: patterns
    reasoning_effort: high
  - name: gemini
    cli: gemini
    model: gemini-2.5-pro
    review_role: edge-cases
    reasoning_effort: high
```

### 5. Parsing in forge.yaml (config.py)

Parse both `review_role` and `reasoning_effort` from the review pool entries
in `forge.yaml`. Pass through to `ModelProfile`. No validation against a fixed
set — unknown roles fall back to the generic prompt, unknown effort values are
ignored.

## Acceptance Criteria

- [ ] `ModelProfile` has `review_role: str | None = None`
- [ ] `build_review_prompt()` accepts `review_role` and produces role-specific
      "Your Role" sections for `correctness`, `patterns`, and `edge-cases`
- [ ] `build_review_prompt()` with `review_role=None` or unknown value produces
      the current generic prompt (backward compatible)
- [ ] Coordinator builds per-reviewer prompts and passes them to the pool
- [ ] `run_agent_pool()` accepts a list of prompts (one per profile)
- [ ] `forge.yaml` parsing reads `review_role` and `reasoning_effort` from pool entries
- [ ] `runner.py` injects `--config` thinking flags for gemini profiles with `reasoning_effort` set
- [ ] `runner.py` injects `--reasoning-effort` flag for codex profiles with `reasoning_effort` set
- [ ] Claude profiles: `reasoning_effort` is silently ignored
- [ ] `reasoning_effort=None`: no extra flags added for any CLI type
- [ ] Existing tests pass without modification
- [ ] New tests verify role-specific prompt content for each built-in role
- [ ] New tests verify fallback behavior for unknown roles
- [ ] New tests verify gemini argv includes correct `--config` flags for each effort level
- [ ] New tests verify codex argv includes `--reasoning-effort` flag
- [ ] New tests verify claude argv is unchanged regardless of `reasoning_effort`
