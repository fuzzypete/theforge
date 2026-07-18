# Preflight partial-evidence artifact

When the preflight agent fails — timeout, crash (SIGKILL), stuck-pattern
termination, or a non-zero exit with no structured verdict — the exploration it
already performed is not zero-value work. In the motivating incident (#332) a
preflight agent spent $2.54 reading adapter files and tracing a code path before
crashing; that exploration correctly identified the relevant files, and it was
thrown away. The **partial-evidence artifact** preserves that exploration so the
PLAN phase can consume it instead of starting from scratch.

This artifact is produced *only* on a failed preflight run (`success is False`).
A successful preflight already surfaces its analysis through `preflight_output`
and `likely_files`; this artifact fills the gap the failure path left open.

## Where it lives

The artifact is stored in three places, mirroring the existing preflight
failure fields (`degraded` / `degraded_reason` / `failure_action`):

1. **On run state** — `CoordinatorState.preflight_partial_evidence` (the
   serialized dict, or `None` when preflight succeeded / the failed run left
   nothing observable).
2. **In the audit trail** — under the audit record's `preflight` block as
   `partial_evidence`, alongside the failure fields it accompanies. It rides in
   the record's `raw_json`; no audit-record schema bump is required.
3. **As a per-run log artifact** — `.forge/logs/.../preflight-partial-evidence.yaml`
   (written next to `preflight.yaml` / `preflight-raw.log`), and inline inside
   `preflight.yaml` under the `partial_evidence` key.

## Contract

Defined by `PreflightPartialEvidence` in
`src/theforge/coordinator/preflight_evidence.py`. Serialized form:

```yaml
partial_evidence:
  files_inspected:            # list[str] — unique file paths the agent read/edited
    - src/theforge/runners/adapters/anthropic.py
    - src/theforge/coordinator/plan_flow.py
  tool_calls:                 # list — ordered tool activity
    - tool: Read
      target: src/theforge/runners/adapters/anthropic.py
    - tool: Grep
      target: plan_review
    - tool: Read
      target: null            # a tool call whose target could not be determined
  partial_conclusion: >-      # str | null — last substantive agent text, if captured
    The adapter path routes through _resolve_model_info; the plan-review
    tracing pointed at ...
  failure_reason: "TIMEOUT: Agent exceeded 300s limit"  # str | null — result.output marker
  failure_code: timeout       # str | null — stable identifier (timeout, stuck_pattern, ...)
  exit_code: -9               # int | null
  cost_usd: 2.54              # float | null — money already spent
  duration_s: 301.2           # float | null — wall-clock before failure
```

### Field semantics

| Field | Meaning |
| --- | --- |
| `files_inspected` | Unique, de-duplicated file paths the agent actually read or edited. Derived from `tool_calls` where the tool is a file-inspection tool (Read/Edit/Write/NotebookEdit/…). Search patterns (Grep/Glob) are excluded here — they still appear in `tool_calls`. |
| `tool_calls` | The ordered sequence of observed tool calls, each `{tool, target}`. `target` is the file path or search pattern, or `null` when it could not be determined. |
| `partial_conclusion` | The last substantive text the agent emitted before dying (truncated). `null` when none was captured. |
| `failure_reason` | The failure marker on the result's `output` (e.g. the timeout/stuck marker), truncated. |
| `failure_code` | Machine-readable failure identifier from the runner (`timeout`, `stuck_pattern`, …), or `null`. |
| `exit_code` / `cost_usd` / `duration_s` | Failure metadata: raw exit code, money spent, wall-clock elapsed. |

The artifact is **omitted** (`None`) when it would be empty — i.e. the agent
died before making any observable tool call *and* left no partial conclusion.

## Provider coverage

Tool-trace capture depends on the runner streaming tool activity. The Claude
runner streams `tool_use` events and retains them on every exit path (success,
timeout, stuck, no-result), so `files_inspected` / `tool_calls` are populated
for Claude preflight runs. Single-shot CLIs that do not stream (Gemini, Codex)
leave `tool_trace` empty; for those providers the artifact still captures the
failure metadata and any `partial_conclusion` recoverable from `output`, but not
the file/tool list. Extending capture to streaming Gemini/Codex is a follow-on.

## Consumption by the PLAN phase

When `state.preflight_partial_evidence` is present, `build_plan_prompt` receives
a pre-rendered markdown block (via `render_partial_evidence`) as a
`## Partial Evidence from a Failed Preflight` section. It lists the files already
inspected, the tool calls made, and any partial conclusion, with an explicit
instruction to verify anything relied upon since the exploration is incomplete.
This is advisory — the plan agent is free to ignore it — and independent of the
success-path `## Preflight Analysis` section.
