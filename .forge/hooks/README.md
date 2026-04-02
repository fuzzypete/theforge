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
