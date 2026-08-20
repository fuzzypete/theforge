Status: record (2026-08-20, issue #2608)

# Prior-Run Selection Replay Record

Corpora replayed:
- `theforge` root: `/Users/pwickersham/src/theforge/.forge/worktrees/issue-2608` — 20 completed summary-backed stories available, replayed subset: `73d7de156730` (`issue-2542`).
- `hdp` root: `/Users/pwickersham/src/hdp` — 4 completed summary-backed stories available, replayed subset: `31e0c2cf5e74` (`issue-339`).

Historical-fidelity note:
- Every replay rebuilt `.forge/knowledge/index.yaml` inside a disposable temp clone checked out at the story's recorded `changed_files.base_ref` and copied in only summaries whose `generated_at` was before the replayed story's `timing.started_at`.
- The replayed audit records do not persist `preflight_likely_files` or `plan_structured`, so all six phase replays are marked `replayed_missing_file_list` and ran `select_prior_runs(..., file_list=None)` instead of substituting post-run changed files.

## Aggregate

- Stories with at least one candidate: `2/2`
- First qualifying signal across de-duplicated story/prior-run matches:
  - `domain_match(backend)`: `3`
  - `story_match`: `3`
- Useful advice hidden by the `3`-candidate cap: `3` phase replays
- Useful advice hidden by the rendered-claim cap: `1` phase replay

## Fence-Parsing Probe

File-only probes did not co-surface the two fence-parser summaries.

- Probe on `src/theforge/coordinator/diagnose_flow.py`: offered `1a6b6e18d232` via `file_overlap(...)`; did not offer `73d7de156730`.
- Probe on `src/theforge/coordinator/preflight.py`: offered `73d7de156730` via `file_overlap(...)` and `dir_overlap(...)`; did not offer `1a6b6e18d232`.
- Co-surface result: `0/2`.

## Story Records

### `theforge` — `73d7de156730` / `issue-2542`

Selected candidates were the same in all three phases: `cf857907b6ed`, `eb45f2b9ab9f`, `27bb13e86070`.
Overflow candidates were the same in all three phases: `6b45fea7f887`, `1a6b6e18d232`, `1f11fc786002`.
Normal exclusions also recorded two inadmissible summaries: `885a94fa87bb` and `986e0edf14c6` as `inadmissible(source_run_tainted, relevance_indeterminate)`.

Useful judgments:
- `plan`: rendered `27bb13e86070#a4b38c83b77e` would have widened the plan to update every persisted audit/schema surface together.
- `plan`: rendered-claim cap displaced `27bb13e86070#646efdf2d483`, which would have highlighted the durable-state inheritance risk.
- `plan`: candidate cap displaced `1a6b6e18d232#a638c52c19c7` and `1a6b6e18d232#577b31b8f17b`, both judged plan-helpful for the parser and partial-reason aspects of the bug.
- `dev`: rendered `27bb13e86070#a4b38c83b77e` would have helped keep audit record, summary, and schema surfaces aligned during implementation.
- `dev`: candidate cap displaced `1a6b6e18d232#a638c52c19c7` and `1a6b6e18d232#577b31b8f17b`, both judged implementation-helpful.
- `review`: rendered `27bb13e86070#e5a8209c4d76` would have prompted checking the canonical run record for the new knowledge-summary provenance block.
- `review`: candidate cap displaced `1a6b6e18d232#7176627f6db5`, `#f95e52122a41`, and `#07faf4e8f6d9`, all judged verification-helpful.

Per-phase cap summary:
- `plan`: useful candidate-cap truncation `yes`; useful claim-cap truncation `yes`.
- `dev`: useful candidate-cap truncation `yes`; useful claim-cap truncation `no`.
- `review`: useful candidate-cap truncation `yes`; useful claim-cap truncation `no`.

### `hdp` — `31e0c2cf5e74` / `issue-339`

Selected candidates were the same in all three phases: `13a56e534cb5`, `18ab832e125c`, `f8e498af39be`.
There were no overflow candidates and no exclusions.

Useful judgments:
- `plan`: `13a56e534cb5#67a354eb7358` and `#aec00eec7681` would have helped frame the work as one authoritative mapping plus a schema-surface regression guard.
- `dev`: `13a56e534cb5#67a354eb7358`, `#aec00eec7681`, and `18ab832e125c#d738482f297e` would have helped implement the authoritative surface and keep the schema resource derived from the same source.
- `review`: `13a56e534cb5#c93eb6375669` and `18ab832e125c#670c2eaffbdd` would have improved verification by checking fresh-session visibility and schema-resource freshness.

Per-phase cap summary:
- `plan`: useful candidate-cap truncation `no`; useful claim-cap truncation `no`.
- `dev`: useful candidate-cap truncation `no`; useful claim-cap truncation `no`.
- `review`: useful candidate-cap truncation `no`; useful claim-cap truncation `no`.

## Deferred Questions

- Controlled vocabulary: no replayed match qualified first on `pattern_match`; pattern tags only co-scored behind `domain_match(...)` or `story_match`. On this evidence, a shared controlled vocabulary is still a deferred optimization rather than a demonstrated recall need.
- Recurrence linking: the two fence-parser summaries did not co-surface on file-only probes. If cross-run recurrence between diagnose parsing and preflight parsing matters, it needs an explicit linkage mechanism; the current selector does not already surface them together.
- Budget sizing: the existing budgets are binding on this subset. The `3`-candidate cap hid useful advice in all three `issue-2542` phase replays, and the rendered-claim cap hid one more useful plan claim. The current tuning should not be treated as comfortably slack.
