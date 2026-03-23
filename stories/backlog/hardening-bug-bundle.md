---
name: "Hardening bug bundle — six fixes for release readiness"
slug: hardening-bug-bundle
pytest_target: tests/
---

# Hardening Bug Bundle

## Problem

Six known bugs and gaps block a stable release. Each is small individually
but they interact: agent startup failures silently waste review cycles,
adaptive overrides don't protect all roles, stale venvs break API agent
selection, and the stage-aware wiring has 5 skipped tests.

## Fixes

### 1. Fail-fast on agent startup failure (#125)

When the runner returns an immediate failure (Unknown CLI: None, missing API
key), the coordinator must abort the phase and ESCALATE immediately — not
silently continue to gate and review with zero work done.

- `AgentResult` needs a `startup_failure: bool` field (or similar sentinel)
- Runner sets it when the agent couldn't start at all
- Coordinator checks it before proceeding to VALIDATE
- On startup failure: skip VALIDATE/REVIEW, go straight to ESCALATE
- Clear error in log: "DEV aborted: no agent available (reason)"

### 2. Default review_pool treated as explicit override (#114)

`config.py` line 786 falls back to `[DEFAULT_REVIEW_PROFILE]` when no
review_pool is configured. The coordinator sees non-empty review_pool and
marks it as explicit, blocking adaptive reviewers for classic configs.

- Add `review_pool_is_default: bool` to ForgeConfig
- Set True only on the fallback path
- Coordinator checks the flag before marking review_pool as explicit

### 3. Planner override not protected (#115)

Adaptive assignment unconditionally replaces the planner when enabled, even
if the user explicitly set `plan.model` in forge.yaml.

- Add `plan_model_is_default: bool` to ForgeConfig
- Check it before applying adaptive planner
- Add "planner" to _explicit_roles when plan.model is explicitly configured

### 4. Stale worktree venvs (#117)

`setup_command` uses `test -d .venv ||` guard, so existing worktrees keep
old venvs missing new extras (e.g. `.[all]`).

- Split venv creation guard from package installation
- Always run `pip install`, only skip `python -m venv`
- Or: hash setup_command, re-run when it changes

### 5. Stage-aware coordinator wiring (#110)

`--from` and `--until` flags exist in CLI and coord_state but the coordinator
doesn't check `start_phase`/`stop_phase` during the phase loop. 5 tests are
skipped with `reason="stage-aware coordinator wiring pending"`.

- Wire start_phase: skip phases before it
- Wire stop_phase: return clean result after it
- Remove skip markers from the 5 tests

### 6. Persist handoff to logs (#106)

After gate reads handoff.yaml, copy it to `.forge/logs/<slug>/handoff-iter-<N>.yaml`
for durable post-mortem access.

- Add copy step in coord_phases after gate read
- Create parent dirs if needed
- Best-effort (try/except, don't block pipeline)

## Acceptance criteria

- Agent startup failure → immediate ESCALATE, no gate/review on zero work
- Default review_pool is not treated as explicit override
- Explicit plan.model in forge.yaml is preserved when adaptive is enabled
- Package installation runs even when .venv exists
- --from and --until flags work in the coordinator (5 skipped tests pass)
- Handoff copied to .forge/logs/ after each dev iteration
- All existing tests pass (including the 5 previously skipped)
