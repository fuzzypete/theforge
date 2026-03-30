# Brief: Plan Review Finding Identity — Canonicalizing Findings Across Attempts

## Problem

When a plan is rejected multiple times, each reviewer phrases the same
underlying objection differently. The coordinator needs to detect "same issue
family surviving across attempts" to mechanically distinguish patch-worthy
rejections from stuck-in-a-hole divergence. Without finding identity, the
coordinator can't tell if attempt 2's three P1s are the same problem as
attempt 1's two P1s or entirely new issues.

## Real example

forge check-config was rejected 4 times. Every rejection was about the same
thing: the planner kept trying to thread a `strict_auth` parameter through
`load_config` and its callers, and the reviewers kept finding more callers
that were missed. Four different phrasings of "this parameter doesn't exist
where you're calling it." The coordinator treated each as a fresh rejection
and burned $3 on four patch attempts that all dug the same hole deeper.

## What the coordinator needs to decide

Given findings from attempt N and findings from attempt N-1:
- Are any findings in N the same issue family as findings in N-1?
- Is the set of unresolved issue families growing, shrinking, or stable?
- Has a specific issue family survived 2+ attempts? (→ trigger backtrack)

## Constraints

- **No LLM in the coordinator loop for process decisions.** The coordinator
  is pure Python. Finding identity must be computable without calling an LLM
  at decision time. (Pre-computing identity at review merge time is fine —
  that's reviewer output processing, not process control.)
- **Reviewers already emit structured findings** with severity, file, line,
  description, and suggestion fields. Any identity scheme should work with
  this existing schema.
- **False positives (grouping unrelated findings) are worse than false
  negatives (missing a match).** A false positive could trigger premature
  backtrack on a plan that's actually making progress. A false negative
  just means one more patch attempt.
- **Must work across different reviewer models.** codex-reviewer and
  deepseek-reviewer describe the same issue very differently.
- **Plan review findings reference plan content, not code.** Unlike dev
  review findings (which reference files/lines in code), plan review
  findings reference steps, parameters, and architectural decisions in
  the plan text.

## Design space to explore

1. **Structural matching**: Extract decision markers from findings
   (function names, parameter names, file paths mentioned) and match on
   those. Cheap, deterministic, but brittle if reviewers describe things
   at different abstraction levels.

2. **Finding tags/themes**: Add an optional `theme` or `decision_marker`
   field to the plan review schema. Reviewers emit a short canonical tag
   (e.g. `strict_auth_threading`). Match on tags. Requires schema change
   and reviewer prompt update. Risk: reviewers may be inconsistent.

3. **Embedding similarity**: Embed finding descriptions and cluster across
   attempts. Requires an embedding model at review merge time. More robust
   to phrasing variation but adds a dependency and latency.

4. **Hybrid**: Extract structural markers first (cheap), fall back to
   embedding similarity for findings that don't have clear markers.

5. **Something else entirely** — maybe the identity should be computed
   from the plan diff rather than the findings. If the same section of
   the plan keeps getting rejected, that's the identity signal.

## Questions

- What's the minimum viable version that catches the `strict_auth` case?
- How do we handle findings that split or merge across attempts (one broad
  finding in attempt 1 becomes three specific findings in attempt 2)?
- Should identity be per-finding or per-issue-family (cluster)?
- Is there a way to do this that doesn't require an embedding model and
  still handles cross-model phrasing differences?
