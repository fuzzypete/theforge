---
name: "Dev agent generalizes pattern fixes from review findings"
slug: dev-generalize-from-findings
pytest_target: tests/
---

# Dev Generalizes Pattern Fixes from Review Findings

## Problem

When a reviewer flags a bug at a specific location (e.g., "line 47 doesn't set
`saveBlocked = true` in the error handler"), the dev agent fixes exactly that
location — and nothing else. If the same pattern bug exists at lines 52 and 68,
those survive to the next review cycle.

This burns review cycles on what a human developer would fix in one pass. Observed
in HDP: a legitimate P1 was caught 3 times in a row, each time at a different
branch of the same error-handling pattern. Dev fixed each cited instance but never
audited the pattern across the codebase. The cycle limit hit before resolution.

The dev is doing what it's told — fix the finding — but not what's needed: step
back and ask "where else does this pattern apply?"

## Solution

Two layers, both in the fix/feedback prompt path:

### Layer 1: Static prompt instruction (always active)

Add to the fix prompt template an instruction like:

> If a finding describes a pattern bug (e.g., missing error handling, inconsistent
> state management, repeated logic error), audit ALL code paths for the same issue
> — not just the cited location. Fix every instance in one pass.

This nudges the dev to generalize on every cycle. Low cost, high leverage.

### Layer 2: Coordinator-injected meta-instruction (cycle 2+)

If the finding classifier detects that cycle N+1 has a finding with high Jaccard
similarity to a cycle N finding but at a DIFFERENT file/line, inject an explicit
meta-instruction into the fix prompt:

> PATTERN ALERT: This is cycle {N} with a similar finding to a previous cycle
> (same pattern, different location). Previous: {file}:{line} — {description}.
> Current: {file}:{line} — {description}. Audit ALL code paths for this pattern
> before fixing. Do not fix only the cited instance.

This kicks in only when the dev demonstrably failed to generalize. The finding
classifier already tracks fingerprints, cycle persistence, and Jaccard overlap —
this reuses that infrastructure.

### Detection heuristic for "same pattern, different location"

A finding from cycle N+1 matches a "pattern sibling" from cycle N when:
- Same severity (both P1)
- Different file OR different line (not the same exact location)
- Jaccard token overlap on description >= JACCARD_THRESHOLD (0.5)
- The prior finding was marked `fixed` (dev addressed the old one but a new
  instance appeared)

This is distinct from `unresolved` (same location, same finding persists) and
`regression` (finding in changed files). It's a new disposition: `pattern_sibling`.

## Acceptance criteria

- Fix prompt includes a static instruction to generalize from pattern findings
- When a finding matches a `fixed` finding from the prior cycle at a different
  location with high description similarity, the coordinator injects a
  pattern-alert meta-instruction into the fix prompt
- The finding classifier assigns `pattern_sibling` disposition to findings that
  match this heuristic
- `pattern_sibling` findings are blocking (same as `regression`) — they indicate
  incomplete fixes
- Tests cover: pattern sibling detection, meta-instruction injection, static
  prompt inclusion
