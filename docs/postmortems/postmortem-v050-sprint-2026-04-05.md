# Postmortem: v0.5.0 Sprint Run — 2026-04-05

**Author:** Claude Opus 4.6 (interactive postmortem analysis)
**Sprint:** v0.5.0
**Sprint name:** `v0.5.0`
**Started:** 2026-04-05 01:12 PDT (08:12 UTC)
**Finished:** 2026-04-05 02:23 PDT (09:23 UTC)
**Duration:** ~72 minutes
**Budget:** $150.00 | **Spent:** $40.53 (Claude costs only; Gemini/Codex report $0)
**Stories:** 13 total | 6 DONE | 7 ESCALATE | 0 SKIP
**Config at sprint start (190c4bb):** dev=gpt-5.4, preflight=gemini-2.5-pro (API), review=[o4-mini, gemini-2.5-pro], plan=claude-sonnet-4-6 (CLI)
**Parallelism:** max_parallel=4

---

## Summary

The sprint completed 6 of 13 stories and spent $40.53 of a $150 budget. All 7 escalations trace back to two root causes:

1. **Preflight regression from #424 (submit tool gating).** Removing the submit tool from preflight left Gemini API with no structured exit path. Gemini defaulted to review-format output (`REQUEST_CHANGES`, `APPROVE`) instead of preflight verdicts (`PROCEED`, `ALREADY_DONE`, `BLOCKED`). The coordinator silently fell through to `PROCEED` with **empty `likely_files`** on 14 of 19 stories, making collision detection completely blind for the entire sprint.

2. **Post-dev-phase `.forge/handoff.yaml` re-commit.** `_deindex_forge_artifacts` runs at workspace setup but not after the dev phase. Dev agents re-committed `handoff.yaml`, causing rebase conflicts on merge that masked otherwise-valid implementations.

These two bugs account for 3 of the 7 escalations directly (merge/rebase failures from blind collision detection and artifact commits). The remaining 4 escalations are genuine dev-agent failures (stuck review loops, hard convention violations) that would benefit from investigation but are not systemic.

---

## Detailed Analysis

### Root Cause 1: Preflight Gemini API returns review verdicts (14/19 stories affected)

**What happened:** Issue #424 ("gate submit tools to review phases only") was merged at 01:04 PDT, 8 minutes before the sprint started. The fix correctly removed `submit_review` and `submit_plan_review` tools from preflight and dev phases via `_NO_SUBMIT_PHASES = {"preflight", "dev"}` and assigned `noop_finalizer` instead of the review finalizer.

However, the Gemini API preflight profile (`provider: google, model: gemini-2.5-pro`) runs in **agent loop mode** (because `allowed_tools` is non-empty — it has Read, Bash, Glob, Grep). In loop mode without a submit tool, Gemini has no structured mechanism to deliver its final answer. When Gemini finishes reading the codebase and is ready to respond, the `noop_finalizer` provides no guidance on output format. Gemini defaults to producing review-style YAML with `verdict: REQUEST_CHANGES` or `verdict: APPROVE`.

**Evidence from preflight.yaml files across all 19 stories:**

| Verdict returned | Count | Stories |
|---|---|---|
| Valid (`PROCEED`, `ALREADY_DONE`, `BLOCKED`) | 5 | #132, #220, #226, #255, #378 |
| `REQUEST_CHANGES` (review verdict) | 10 | #256, #326, #334, #348, #351, #355, #360, #366, #391, #25 |
| `APPROVE` (review verdict) | 2 | #26, #227 |
| Agent failure (exit=1) | 2 | #322, #366 |

All 14 invalid-verdict stories had `likely_files: []`, rendering collision detection inert.

**Why the 5 valid stories worked:** Unknown without raw agent output, but likely related to response timing — if Gemini happened to produce preflight-format YAML before the loop manager intervened with finalization, the parser would accept it.

**Why it wasn't caught:** The `_parse_preflight_verdict` function treats unrecognized verdicts as non-fatal: `return "PROCEED", f"Unknown preflight verdict {verdict!r}; proceeding anyway."` This was a deliberate design choice (cheaper to try DEV than to stall on broken preflight) but it silenced a systemic signal. The coordinator logged `Unknown preflight verdict 'REQUEST_CHANGES'; proceeding anyway` for 10 stories and nobody saw it because the sprint was unattended.

**Impact chain:**
- Empty `likely_files` → `compute_synthetic_edges` found zero collisions → no serialization injected
- Stories that should have been serialized ran in the same batch
- Parallel merges moved main, causing rebase conflicts and stale-branch failures on merge

### Root Cause 2: Dev agent re-commits `.forge/handoff.yaml` after workspace deindex

**What happened:** `_deindex_forge_artifacts` (workspace.py:342) removes `.forge/handoff.yaml` and `.forge/trajectory.yaml` from the git index at workspace setup. It is called at 4 points, all during WORKSPACE phase creation/resume. After the dev phase, there is no deindex call.

The dev agent for #227 made real implementation commits (`cf64942 — fix(coordinator): avoid duplicate PRs on sprint resume`) touching `preflight_flow.py`, `completion.py`, and test files. It then separately committed `.forge/handoff.yaml` as `d87cc9e`. When the sprint attempted to rebase #227 onto main (which had moved due to 6 other stories merging), the rebase conflict was on `handoff.yaml` — not on the real code.

**Evidence:** `git show d87cc9e --stat` shows only `.forge/handoff.yaml` modified.

**Impact:** The real implementation in `cf64942` is valid and review-approved but could not merge. The handoff commit masked a recoverable story as an ESCALATE.

---

### Per-Story Escalation Detail

#### #26 — Commit-centric review prompt
- **Failure:** `gh pr merge` blocked — `the base branch policy prohibits the merge`
- **CI status:** `gate (3.11)` FAILURE — `ModuleNotFoundError: No module named 'openai'` in `test_runner_submit_tool_gating.py` (6 tests)
- **Root cause:** Dev agent changed runner code that broke the submit tool gating tests added by #424. The tests import `openai` without mocking it. CI failed, branch protection blocked the merge.
- **Category:** Real test failure in dev agent output. Needs re-run or manual fix.

#### #227 — Sprint resume creates duplicate PRs
- **Failure:** Rebase conflict on `.forge/handoff.yaml` during merge
- **Root cause:** Root Cause 2 (handoff re-commit). Real implementation in `cf64942` is intact and review-approved.
- **Recovery:** Strip `d87cc9e`, rebase `cf64942` onto main, re-merge.
- **Contributing factor:** Root Cause 1 (blind collision detection) — #227 ran in batch 0 with #25 and #256 without serialization.

#### #360 — `--until plan` flag has no effect
- **Failure:** `pull request create failed: GraphQL: No commits between main and feat/issue-360`
- **Root cause:** Branch is 1 commit "ahead" of main but that commit IS a main commit (48f095d). The worktree has only an unmerged `handoff.yaml` in working tree. Dev agent produced review-approved work but commits were lost or applied to the wrong ref.
- **Category:** Git state corruption, likely related to parallel worktree operations. Needs full re-run.

#### #322 — Handoff integrity
- **Failure:** `Dev retry produced no changes — escalating to avoid re-reviewing identical code`
- **Review history:** Cycle 1: 6 P1s. Cycle 2: 9 P1s (increasing).
- **Cost:** $7.91 — most expensive escalation.
- **Category:** Dev agent unable to address reviewer feedback. Story may be too complex or underspecified. Needs investigation of review findings before re-run.

#### #366 — Release-branch support
- **Failure:** `Dev retry produced no changes — escalating to avoid re-reviewing identical code`
- **Review history:** Cycle 1: 3 P1s. Cycle 2: 6 P1s (increasing).
- **Cost:** $9.05 — highest cost story in the sprint.
- **Category:** Same pattern as #322. Dev agent stuck. Gemini reviewer failed in cycle 1 (parse failure, retried). Needs investigation of review findings before re-run.

#### #326 — Model preference lists for best-available provider fallback
- **Failure:** `Hard convention violations after 3 attempts`
- **Never reached review.** Dev agent could not produce convention-compliant code in 3 attempts.
- **Category:** Convention or story issue. Need to check which convention was violated and whether it's a false positive.

#### #348 — Concurrent sprint launches can double-spend against same worktrees
- **Failure:** `Hard convention violations after 3 attempts`
- **Same pattern as #326.** Never reached review.
- **Category:** Convention or story issue. Same investigation needed.

---

## Proposed Action Items

### P0 — Fix before next sprint run

1. **Fix Gemini API preflight exit path (#435)**
   Gemini in loop mode with no submit tool needs a structured way to deliver preflight output. Options:
   - (a) Add a `submit_preflight` tool with the correct schema (verdict, reason, complexity, sufficiency, work_type, likely_files)
   - (b) Run preflight in plain-text mode (bypass the loop, single-shot call) and rely on YAML parsing
   - (c) Add a preflight-specific finalizer that extracts the YAML from the last text response on timeout

   Option (b) is simplest and matches how preflight worked before #424. Option (a) is cleanest long-term.

2. **Make invalid preflight verdicts a hard failure (#435)**
   Change `_parse_preflight_verdict` to return `ESCALATE` (not `PROCEED`) when the verdict is unrecognized. A model returning `APPROVE` from preflight is confused — its complexity, sufficiency, and likely_files are unreliable. Proceeding silently wastes downstream spend.

3. **Deindex `.forge/handoff.yaml` post-dev-phase before merge (#434)**
   Add `_deindex_forge_artifacts(workspace_path)` call after the dev phase completes and before rebase/merge. This prevents dev agents from accidentally introducing forge artifacts into story commits.

### P1 — Investigate before re-running affected stories

4. **Investigate #322 and #366 review findings**
   Pull the actual P1 findings from `review-cycle-1/` and `review-cycle-2/` logs. Determine whether the reviewers are flagging real issues the dev agent can't fix, or whether the P1s are false positives from overly strict review criteria. Increasing P1 counts across cycles (6→9 for #322, 3→6 for #366) suggest the dev agent is introducing new issues while trying to fix old ones.

5. **Investigate #326 and #348 hard convention violations**
   Per Codex's analysis, both failures were `max_module_lines` — `cli.py` at 521 lines and `sprint.py` at 508 lines against a 500-line limit. The convention violation details (rule, file, line count) ARE passed to the dev agent on retry via `state.human_feedback` → `## CRITICAL: Human Feedback` in the fix prompt. The dev agent has the information — it just fails to act on it in 3 attempts. This is a model capability issue: the agent needs to split/extract code to make room but instead keeps trying to shrink-in-place. Investigate whether the dev logs show the agent attempting extraction or just minor edits.

### P2 — Sprint infrastructure improvements

6. **Log raw preflight agent output**
   Currently only the parsed `preflight.yaml` is saved. The raw agent output is discarded. Add a `preflight-raw.log` (or equivalent) to the per-story log directory so postmortems can inspect exactly what the model produced.

7. **Re-run #227 manually**
   The implementation is done (`cf64942`). Strip the handoff commit, rebase onto main, push, and merge. No need to re-run the full pipeline.

8. **Clean up stale worktrees**
   17 worktrees exist. 6 DONE stories have worktrees that should be removed. 2 already-merged stories (#220, #395) have stale worktrees. Run `git worktree remove` for all completed/merged worktrees before the next sprint.
