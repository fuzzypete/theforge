# Epic: Reviewer Quality — Better Reviews, Fewer Wasted Cycles

## Vision

Reviews are the most expensive phase (2 models + synthesis per cycle).
Improve review quality, reduce false P1s, and make reviewer output more
actionable so fewer cycles are needed.

## Stories

### Phase 1: Role specialization
- [x] `reviewer-role-specialization.md` — per-reviewer prompts with
      correctness/patterns/edge-cases lenses, Gemini thinking config,
      codex --reasoning-effort flag

### Phase 2: Review quality
- [ ] `human-review-brief.md` — Opus-generated notification brief
      summarizing what changed and why, for faster human triage
- [ ] False P1 reduction: reviewers that consistently produce P1s that
      get resolved in 1 iteration should have their severity calibrated
      (needs spec)
- [ ] Review diff focus: only send changed functions/tests to reviewer,
      not the entire file diff (needs spec)

## Definition of Done

- Each reviewer in the pool has a distinct focus area
- Human review notifications are actionable without reading full diff
- False P1 rate decreases measurably
