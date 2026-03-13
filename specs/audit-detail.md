---
name: "Audit detail: per-spec campaign audits and review pool diagnostics"
slug: audit-detail
file_scope:
  - src/theforge/coordinator.py
  - src/theforge/campaign.py
  - src/theforge/cli.py
  - tests/test_coordinator.py
pytest_target: tests/test_coordinator.py
---

# Audit Detail

## Problem

Two gaps leave campaign runs effectively unauditable:

### P1: Campaign writes no per-spec audit detail

`campaign.py` calls `run_task()` directly and only records outcome/cost in
`campaign-audit.yaml`. The per-spec `forge_audit.yaml` is only written by
`forge run` (CLI path), never by campaign. After a campaign run:

- No record of which reviewers succeeded or failed
- No record of review verdicts, P1/P2 counts, or findings
- No record of parse retry counts
- No record of gate decisions per iteration
- Codex can fail silently on every review cycle — campaign shows "DONE"

**Observed:** User saw `Pool reviewer failed: codex` log lines scroll past during
a campaign run. Campaign audit shows DONE. No way to confirm Codex ever
contributed a verdict to any spec.

### P2: `forge_audit.yaml` review section missing pool diagnostics

Even in single-spec `forge run`, the reviews section omits:
- Which reviewers were in the pool
- Which succeeded vs failed (exit code, error)
- Whether synthesis ran or was degraded (fell back to single reviewer)
- Individual reviewer verdicts before synthesis

Current `reviews` entry:
```yaml
reviews:
- cycle: 1
  pool_models: []      # always empty list
  successful: []       # always empty list
  failed: []
  synthesized: false
  verdict: APPROVE
  ...
```

`pool_models`, `successful`, and `failed` are present in `ReviewCycleMeta`
but `generate_audit_log()` serializes them as empty lists.

## Design

### Fix 1: Populate review pool fields in `generate_audit_log()`

In `coordinator.py`, `ReviewCycleMeta` already has:
```python
pool_models: list[str]    # profile names in the pool
successful: list[str]     # profile names that returned output
failed: list[str]         # profile names that failed (exit != 0)
synthesized: bool         # whether synthesis agent ran
parse_retries: int        # parse retry count this cycle
```

`generate_audit_log()` must serialize these fields from the actual
`ReviewCycleMeta` objects in `state.review_cycle_metadata`.

Add per-reviewer result detail to each cycle:
```yaml
reviews:
- cycle: 1
  pool_models: [opus, codex]
  successful: [opus]
  failed: [codex]           # exit=1 or timeout
  failed_detail:            # NEW: exit codes / error snippets
    codex: "exit=1"
  synthesized: false        # degraded: only 1 succeeded
  verdict: APPROVE
  summary: "Implementation matches spec"
  p1_count: 0
  p2_count: 2
  parse_retries: 0
  findings: [...]
```

### Fix 2: Campaign writes per-spec audit to worktree

After each `run_task()` call in `campaign.py`, write the full spec audit
to the worktree as `forge_audit.yaml`:

```python
result = run_task(config, task, interactive=interactive, auto_merge=auto_merge)

# Write per-spec audit to worktree for diagnostics
workspace_path = config.project_root / config.workspace.path_pattern.format(
    slug=task.slug
)
if workspace_path.exists():
    audit = generate_audit_log(config, task, result)
    audit_path = workspace_path / "forge_audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
```

This mirrors exactly what `cli.py` does for `forge run`. The audit lands
in `.forge/worktrees/<slug>/forge_audit.yaml` — inspectable after the run.

### Fix 3: Embed review summary in `campaign-audit.yaml`

Add a `reviews` summary to each spec entry in `campaign-audit.yaml`:

```yaml
specs:
- path: specs/review-parse-retry.md
  outcome: DONE
  cost_usd: 5.27
  preflight: PROCEED
  merge: false
  reviews:                      # NEW
  - cycle: 1
    verdict: APPROVE
    pool: [opus, codex]
    successful: [opus]
    failed: [codex]
    p1_count: 0
    p2_count: 0
    parse_retries: 0
```

This makes `campaign-audit.yaml` self-contained for triage without needing
to open individual worktree audits.

### Fix 4: `forge run` also writes to worktree (not just project root)

Currently `forge run` writes `forge_audit.yaml` to the **project root**,
overwriting the previous run. If you run two specs back-to-back, the first
audit is lost.

Write to **both**:
1. `{project_root}/forge_audit.yaml` — convenience, last-run (existing)
2. `{workspace_path}/forge_audit.yaml` — persistent per-spec (new)

## Acceptance Criteria

1. After `forge campaign`, each completed spec's worktree contains a
   full `forge_audit.yaml` with review pool details
2. `campaign-audit.yaml` includes a `reviews` summary per spec showing
   pool, successful, failed reviewers and verdict per cycle
3. `forge_audit.yaml` review entries include `pool_models`, `successful`,
   `failed`, `failed_detail`, and `synthesized` with actual values (not
   empty lists)
4. `forge run` writes audit to both project root and worktree
5. A Codex failure (exit=1) is visible in both the spec-level and
   campaign-level audit — not silently dropped
6. ALREADY_DONE specs in campaigns do not write a worktree audit
   (no worktree was created)

## Test Expectations

In `tests/test_coordinator.py`:

- `test_audit_review_pool_fields_populated` — when pool has 2 reviewers
  and one fails, `generate_audit_log()` returns reviews with correct
  `pool_models`, `successful`, `failed` lists
- `test_audit_failed_reviewer_detail` — failed reviewer includes
  exit code in `failed_detail`
- `test_audit_synthesized_flag` — synthesized=True when synthesis
  agent ran, False when degraded to single reviewer

In `tests/test_campaign.py` (new file or existing):

- `test_campaign_writes_worktree_audit` — after `run_campaign()`,
  worktree contains `forge_audit.yaml`
- `test_campaign_audit_includes_review_summary` — `campaign-audit.yaml`
  has `reviews` list per spec with pool/successful/failed fields

## Out of Scope

- Full findings list in `campaign-audit.yaml` (kept in worktree audit only,
  to keep campaign file scannable)
- Streaming audit writes during the run (audit written once on completion)
