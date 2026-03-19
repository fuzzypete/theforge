---
name: "Human review brief — Opus decision summary in ntfy notification"
slug: human-review-brief
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/task.py
  - tests/test_coordinator.py
pytest_target: tests/
---

# Human Review Brief

## Problem

When forge reaches HUMAN_REVIEW after exhausting review cycles, the ntfy
notification body contains:

```
REQUEST_CHANGES (1 P1, 0 P2) — $3.77  18m 47s
<120 char raw synthesis summary>
Branch: feat/dev-scope-escalation
```

This is not enough to make an informed decision from a phone. The human
must open a terminal, find the log, and read the audit to understand:
- What the agent actually built
- What specifically the P1 is about
- Whether the P1 is legitimate or overly strict
- What approving vs extending vs escalating would mean

## Solution

Before sending the ntfy notification, invoke a **brief agent** (Opus) with
full context to produce a 3-5 sentence decision summary formatted for mobile
reading. Include this brief in the notification body.

### Brief agent input

The brief prompt (`build_human_review_brief_prompt()` in `task.py`) receives:

- Task name and slug
- The spec's acceptance criteria section (not the full spec)
- The synthesis verdict and summary
- All P1 findings (file, line, description, suggestion)
- Review cycle count and total cost so far
- The `extend` option description (one more dev pass)

### Brief agent output

Plain text, 3-5 sentences, structured as:

```
BUILT: <one sentence — what the agent implemented>
CONCERN: <one sentence — what the P1 reviewer found>
ASSESS: <one sentence — is this P1 likely correct, nitpicky, or unclear>
OPTIONS: Approve=<outcome>, Extend=<outcome>, Escalate=<outcome>
```

Target: ~400 chars total to fit comfortably in a push notification.

### Profile

Use a configurable `human_review_brief` profile in `forge.yaml`. If not
configured, fall back to the synthesis profile. Default recommendation:

```yaml
human_review_brief:
  cli: claude
  model: opus
  budget_usd: 0.50
  timeout_seconds: 60
```

Opus is appropriate here: this is a one-shot high-stakes judgment call, runs
once per human review event, and the $0.50 budget cap prevents runaway cost.

### Notification body with brief

Replace the current 3-line body with:

```
REQUEST_CHANGES (1 P1, 0 P2) — $3.77  18m 47s

BUILT: Added SCOPE_BLOCKED sentinel detection in coordinator using last-line
heuristic.
CONCERN: Detection window (≤2 lines after sentinel) may miss multi-file
required-files lists.
ASSESS: Likely overly strict — spec says single-line format; edge case is
theoretical.
OPTIONS: Approve=merge as-is, Extend=one more dev pass, Escalate=abandon+reopen
```

### Coordinator change

In `_run_remote_human_review()`, before calling `_ntfy_publish()`:

1. Call `_run_shell()` (or the agent runner) with the brief profile and prompt
2. Parse the brief output (plain text, no schema needed — just use raw output
   truncated to 800 chars if the agent over-generates)
3. Prepend brief to notification body
4. If brief generation fails or times out (>60s), fall back to current 3-line
   body (fail silently, never block the notification)

## Acceptance Criteria

- [ ] `build_human_review_brief_prompt()` in `task.py` produces a prompt
      with spec criteria, P1 findings, synthesis summary, and options context
- [ ] `human_review_brief` profile parsed from `forge.yaml` if present
- [ ] Falls back to synthesis profile if `human_review_brief` not configured
- [ ] `_run_remote_human_review()` calls brief agent before publishing
- [ ] Brief output (≤800 chars) included in ntfy notification body
- [ ] Brief generation failure falls back silently to current 3-line body
- [ ] Brief timeout (60s) treated as failure → silent fallback
- [ ] Existing tests pass without modification
- [ ] New test: `build_human_review_brief_prompt()` includes P1 findings,
      synthesis summary, and OPTIONS line
- [ ] New test: coordinator notification body includes brief when brief
      agent succeeds
- [ ] New test: coordinator notification body falls back gracefully when
      brief agent returns empty output
