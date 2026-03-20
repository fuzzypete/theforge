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

## forge.yaml configuration

```yaml
hooks:
  post_run: .forge/hooks/post_run.sh
  # post_merge: .forge/hooks/post_merge.sh
  # post_sprint: .forge/hooks/post_sprint.sh
  # pre_run: .forge/hooks/pre_run.sh
  timeout_seconds: 30
```
