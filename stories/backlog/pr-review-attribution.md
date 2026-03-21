---
name: "PR review attribution — per-reviewer GitHub reviews with branch protection"
slug: pr-review-attribution
pytest_target: tests/
---

# PR Review Attribution

## Problem

When `on_approve: pr` is set, theforge creates a PR with a prose summary of the
review in the body. But:

1. **No formal GitHub review object** — branch protection "Required reviews" is not
   satisfied because there's no APPROVED review, only a PR description.
2. **Attribution is flat** — the body lists findings merged together. The per-reviewer
   signal (Claude found X, DeepSeek found Y) is buried or lost.
3. **Not native** — someone looking at the PR sees no review activity, just a
   forge-generated description. Looks unreviewed to anyone who doesn't know theforge.

## Solution

Extend `post_run.sh` to post GitHub review submissions via the API — one COMMENT
review per forge reviewer (preserving attribution), then one final APPROVE review
(satisfying branch protection). All from the forge-bot account.

The PR timeline then reads like a real multi-reviewer review:

```
forge-bot reviewed              claude-reviewer: APPROVE — no P1s. One P2 on line 42...
forge-bot reviewed              deepseek-reviewer: APPROVE — implementation correct...
forge-bot reviewed              gemini-reviewer: APPROVE — edge cases handled well...
forge-bot reviewed  ✓ APPROVED  Merged verdict: APPROVE (0 P1, 2 P2) · $0.81 · 4 reviewers
```

## Design

### Hook layer only — no coordinator changes

All GH API calls live in `post_run.sh`. The coordinator payload already has
everything needed:

```json
{
  "verdict": "APPROVE",
  "slug": "my-story",
  "branch": "feat/my-story",
  "pr_number": 9,
  "summary": "...",
  "findings": [...],
  "reviewers": [
    {
      "name": "claude-reviewer",
      "verdict": "APPROVE",
      "summary": "No P1s. One P2 on line 42...",
      "findings": [...]
    },
    ...
  ]
}
```

### N+1 review submissions

For each reviewer in `reviewers[]`:

```bash
gh api repos/{owner}/{repo}/pulls/{pr}/reviews \
  --method POST \
  --field event="COMMENT" \
  --field body="**${name}** (${model}): ${verdict} — ${summary}\n\n${findings_text}"
```

Final merged review:

```bash
gh api repos/{owner}/{repo}/pulls/{pr}/reviews \
  --method POST \
  --field event="APPROVE" \
  --field body="**Forge verdict: ${verdict}** (${p1_count} P1, ${p2_count} P2) · \$${cost} · ${reviewer_count} reviewers\n\n${merged_summary}"
```

### Line numbers: file-level only

Reviewer findings carry file + line from the schemas, but GitHub inline review
comments require diff positions (not absolute line numbers), and only changed
lines can receive inline comments. To avoid fragile diff-position math:

- Post findings as part of the review body text (file and line in prose: `` `foo.py:42` ``)
- Do not attempt inline diff comments in v1

This preserves location information readably without fighting the GitHub API.

### Guard conditions

Only fire if:
- `verdict == "APPROVE"` (don't post reviews on ESCALATE — no PR exists)
- `pr_number` is present in payload (PR was actually created)
- `gh` CLI authenticated and available
- `reviewers[]` array is non-empty (pool run, not single reviewer)

### Fallback for single-reviewer runs

If `reviewers[]` has only one entry (or is absent), post a single APPROVE review
with that reviewer's findings. No COMMENT + APPROVE split needed.

### `forge init-hooks` update

Update the scaffolded `post_run.sh` to include the PR review attribution block,
guarded by a `FORGE_GH_PR_REVIEWS=1` env var so teams can opt in without breaking
existing issue-filing behaviour.

## Payload additions needed

The current post_run payload (`coord_hooks.py: build_post_run_payload`) does not
include per-reviewer breakdowns or `pr_number`. Two additions required:

1. **`pr_number`** — the GH PR number if a PR was created (already stored in
   `state.pr_url`; parse number from URL or store separately)
2. **`reviewers`** — list of per-reviewer result dicts from `state.review_results`

These are coordinator-side changes (small, in `coord_hooks.py`), but they enrich
the payload for any hook — not just this one.

## Acceptance criteria

- [ ] `build_post_run_payload()` includes `pr_number` (null if no PR)
- [ ] `build_post_run_payload()` includes `reviewers[]` with per-reviewer name,
      verdict, summary, findings
- [ ] `post_run.sh` posts one COMMENT review per reviewer on APPROVE + PR
- [ ] `post_run.sh` posts one final APPROVE review with merged summary
- [ ] Review bodies include file+line references as prose (`` `path/file.py:42` ``)
- [ ] Single-reviewer fallback: one APPROVE review with that reviewer's findings
- [ ] Guard: no reviews posted if `pr_number` is null
- [ ] Guard: no reviews posted if `gh` CLI not available
- [ ] Guard: skip gracefully if `FORGE_GH_PR_REVIEWS` env var not set (opt-in)
- [ ] `forge init-hooks` scaffolds updated `post_run.sh` with PR reviews block
- [ ] Tests for `build_post_run_payload()` cover `pr_number` and `reviewers[]`
- [ ] All existing tests pass
