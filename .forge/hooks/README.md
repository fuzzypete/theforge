# .forge/hooks

Lifecycle hook scripts for TheForge. Place executable scripts here and
reference them in `forge.yaml` under the `hooks:` key.

## post_run.sh — payload schema

The `post_run` hook receives a JSON object on stdin after every `forge run`:

```json
{
  "event": "post_run",
  "verdict": "APPROVE | REQUEST_CHANGES | ESCALATE",
  "slug": "my-feature",
  "branch": "feat/my-feature",
  "summary": "one-line review summary",
  "findings": [
    {
      "severity": "P1 | P2",
      "file": "src/foo.py",
      "line": 42,
      "description": "what is wrong",
      "suggestion": "how to fix"
    }
  ]
}
```

## Finding validation before filing

Every finding body the hook renders is validated against the lifecycle state the
hook declares for it — `needs_operator_action`, the state an untriaged
`bug` + `forge-finding` + `needs-triage` object occupies — before `gh issue
create` runs. A finding whose rendered body would land anywhere else is **not
filed**; the mismatch and its reason codes go to stderr instead. This is what
keeps forge from filing issues its own shape gate refuses.

The check runs `python -m theforge.shape_check.producer`. The hook finds an
interpreter that can import `theforge` by reading the shebang of the installed
`forge` executable, falling back to `python3`/`python` on PATH. Set
`FORGE_PYTHON` to override:

```bash
FORGE_PYTHON=/path/to/venv/bin/python .forge/hooks/post_run.sh < payload.json
```

The check fails closed: **any** nonzero exit — a verdict mismatch, a missing
interpreter, `theforge` not importable — skips that finding. If findings stop
appearing, read the hook's stderr; it names the producer, the declared state and
what could not be satisfied.

## Hook contract

- **stdin**: JSON payload (schema above)
- **exit 0**: success — forge continues normally
- **exit non-zero**: hook failure — forge prints a warning but does not abort

## pre_run.sh — payload schema

Fires before each run starts. **Exit non-zero to abort the run** (the only hook with abort power).

```json
{
  "event":   "pre_run",
  "project": "theforge",
  "slug":    "my-feature",
  "story":   "specs/backlog/my-feature.md",
  "spec":    "specs/backlog/my-feature.md",
  "run_id":  "abc123"
}
```

## post_merge.sh — payload schema

Fires after a story branch is successfully merged.

```json
{
  "event":     "post_merge",
  "project":   "theforge",
  "slug":      "my-feature",
  "branch":    "feat/my-feature",
  "merged_to": "main",
  "run_id":    "abc123"
}
```

## post_sprint.sh — payload schema

Fires after a full sprint completes.

```json
{
  "event":            "post_sprint",
  "project":          "theforge",
  "sprint":           "sprint-name",
  "run_id":           "abc123",
  "total_cost_usd":   1.23,
  "duration_seconds": 3600.0,
  "stories": [
    { "slug": "my-feature", "outcome": "done" }
  ]
}
```

## forge.yaml configuration

```yaml
hooks:
  pre_run: .forge/hooks/pre_run.sh
  post_run: .forge/hooks/post_run.sh
  post_merge: .forge/hooks/post_merge.sh
  post_sprint: .forge/hooks/post_sprint.sh
  timeout_seconds: 30
```
