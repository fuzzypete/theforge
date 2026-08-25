"""Hooks scaffolding command for forge CLI."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

_POST_RUN_SH = """\
#!/usr/bin/env bash
# Reference post_run hook: file GitHub Issues for P1/P2 findings
# Fires after every forge run; creates one issue per finding on ESCALATE or
# APPROVE-with-findings outcomes.
#
# Requires: gh CLI (authenticated), jq
set -euo pipefail

# Guard: gh not installed → warn and exit cleanly
if ! command -v gh &> /dev/null; then
  echo "[forge hook] gh CLI not found — skipping GitHub issue creation" >&2
  exit 0
fi

# Guard: jq not installed → warn and exit cleanly
if ! command -v jq &> /dev/null; then
  echo "[forge hook] jq not found — skipping GitHub issue creation" >&2
  exit 0
fi

payload=$(cat)

verdict=$(echo "$payload" | jq -r '.verdict')
slug=$(echo "$payload" | jq -r '.slug')
branch=$(echo "$payload" | jq -r '.branch')
branch_display="${branch//\\// -> }"
summary=$(echo "$payload" | jq -r '.summary')
findings_count=$(echo "$payload" | jq '.findings | length')

# Ensure the intake label exists before filing findings. --force keeps the
# description aligned without failing when the label already exists.
gh label create "needs-triage" \\
  --description "Forge finding awaiting explicit triage decision" \\
  --color "D4C5F9" \\
  --force >/dev/null 2>&1 || true

# Only act on ESCALATE or APPROVE outcomes (REQUEST_CHANGES = still in review)
if [ "$verdict" != "ESCALATE" ] && [ "$verdict" != "APPROVE" ]; then
  exit 0
fi

# File one GitHub Issue per finding (only when findings exist)
if [ "$findings_count" -gt 0 ]; then
  echo "$payload" | jq -c '.findings[]' | while read -r finding; do
    sev=$(echo "$finding" | jq -r '.severity')
    observed=$(echo "$finding" | jq -r '.observed // ""')
    expected=$(echo "$finding" | jq -r '.expected // ""')
    evidence=$(echo "$finding" | jq -r '.evidence // ""')
    suggestion=$(echo "$finding" | jq -r '.suggestion // ""')

    # Title: [P1] slug: <observed> (truncated to 72 chars)
    raw_title="[${sev}] ${slug}: ${observed}"
    title="${raw_title:0:72}"

    body="**Observed:** ${observed}

**Expected:** ${expected}

**Evidence:** ${evidence}"

    if [ -n "$suggestion" ]; then
      body="${body}

## Suggested approach (non-binding)

${suggestion}

*Non-binding guidance from the reviewer; the dev agent is free to pick a different fix.*"
    fi

    footer="*Filed by theforge post_run hook · story \\`${slug}\\`"
    footer="${footer} · branch \\`${branch_display}\\` · ${verdict}.*"
    body="${body}

---
${footer}"

    gh issue create \\
      --title "$title" \\
      --body "$body" \\
      --label "bug" \\
      --label "forge-finding" \\
      --label "needs-triage" \\
      --label "$(echo "$sev" | tr 'A-Z' 'a-z')" || true
  done
fi

# ── PR Review Attribution ─────────────────────────────────────────────
# Opt-in: set FORGE_GH_PR_REVIEWS=1 to enable posting per-reviewer GitHub reviews.
# The gh api calls below use `repos/{owner}/{repo}` — gh CLI resolves these
# automatically to the current repository when run from within the repo.
# See: https://cli.github.com/manual/gh_api (the {owner}/{repo} tokens are
# expanded by gh from the remote origin URL automatically).
[ -z "${FORGE_GH_PR_REVIEWS:-}" ] && exit 0

# Only post reviews on APPROVE
[ "$verdict" != "APPROVE" ] && exit 0

pr_number=$(echo "$payload" | jq -r '.pr_number // "null"')
[ "$pr_number" = "null" ] && exit 0

reviewers_count=$(echo "$payload" | jq '(.reviewers // []) | length')

if [ "$reviewers_count" -le 1 ]; then
  # Single-reviewer (or absent): post one APPROVE with that reviewer's findings
  reviewer_body=$(echo "$payload" | jq -r '
    (.reviewers // []) | if length == 0 then
      "Approved by theforge review pool.\\n\\n*Posted by theforge post_run hook.*"
    else
      .[0] as $r |
      "**Reviewer:** \\($r.name) (`\\($r.model)`)\\n\\n**Summary:** \\($r.summary)\\n\\n" +
      (if ($r.findings | length) > 0 then
        "**Findings:**\\n" +
        ($r.findings | map(
          "- [`\\(.file):\\(.line // "?")`] [\\(.severity)] \\(.observed // .description // "")"
        ) | join("\\n")) + "\\n\\n"
      else "" end) +
      "*Posted by theforge post_run hook.*"
    end
  ')
  jq -n --arg body "$reviewer_body" --arg event "APPROVE" \\
    '{body: $body, event: $event}' | \\
    gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \\
      --method POST \\
      --input - || true
else
  # Multi-reviewer: post one COMMENT per reviewer, then one final APPROVE
  echo "$payload" | jq -c '.reviewers[]' | while read -r reviewer; do
    r_name=$(echo "$reviewer" | jq -r '.name')
    r_model=$(echo "$reviewer" | jq -r '.model')
    r_verdict=$(echo "$reviewer" | jq -r '.verdict')
    r_summary=$(echo "$reviewer" | jq -r '.summary')
    r_findings_count=$(echo "$reviewer" | jq '.findings | length')

    comment_body="**Reviewer:** ${r_name} (\\`${r_model}\\`)
**Verdict:** ${r_verdict}
**Summary:** ${r_summary}"

    if [ "$r_findings_count" -gt 0 ]; then
      findings_text=$(echo "$reviewer" | jq -r '
        .findings | map(
          "- [`\\(.file):\\(.line // "?")`] [\\(.severity)] \\(.observed // .description // "")"
        ) | join("\\n")
      ')
      comment_body="${comment_body}

**Findings:**
${findings_text}"
    fi

    comment_body="${comment_body}

*Posted by theforge post_run hook.*"

    jq -n --arg body "$comment_body" --arg event "COMMENT" \\
      '{body: $body, event: $event}' | \\
      gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \\
        --method POST \\
        --input - || true
  done

  # Final APPROVE with merged summary
  approve_body="**theforge review pool APPROVED** — ${summary}

*Posted by theforge post_run hook.*"

  jq -n --arg body "$approve_body" --arg event "APPROVE" \\
    '{body: $body, event: $event}' | \\
    gh api "repos/{owner}/{repo}/pulls/${pr_number}/reviews" \\
      --method POST \\
      --input - || true
fi
"""

_HOOKS_README = """\
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
  "pr_number": 42,
  "findings": [
    {
      "severity": "P1 | P2",
      "file": "src/foo.py",
      "line": 42,
      "observed": "one-sentence behaviour-only description",
      "expected": "category-level rule in flowing prose",
      "evidence": "file path + line/anchor",
      "suggestion": "optional non-binding fix sketch (may be empty string)"
    }
  ],
  "reviewers": [
    {
      "name": "reviewer-profile-name",
      "model": "claude-sonnet-4-6",
      "verdict": "APPROVE | REQUEST_CHANGES",
      "summary": "one-line summary from this reviewer",
      "findings": [...]
    }
  ]
}
```

`pr_number` is an integer extracted from the merge PR URL, or `null` if the run
did not result in a PR merge. `reviewers` contains per-reviewer attribution from
the last review cycle; empty list if no reviewers ran.

## PR Review Attribution

Set `FORGE_GH_PR_REVIEWS=1` to enable automatic posting of GitHub PR reviews
after an APPROVE outcome. Requires `gh` CLI authenticated and `pr_number` to be
non-null.

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
"""


def resolve_hook_path(project_root: Path, hook_command: str | None) -> Path | None:
    """Return the script path for a configured hook command, if it is path-like."""
    if not hook_command:
        return None
    try:
        parts = shlex.split(hook_command)
    except ValueError:
        return None
    if not parts:
        return None
    path = Path(parts[0])
    if not path.is_absolute():
        path = project_root / path
    return path


_STATIC_LABEL_RE = re.compile(r"--label\s+(?:\"([^\"$`\\]+)\"|'([^'$`\\]+)'|([A-Za-z0-9_.:-]+))")
_LABEL_CREATE_RE = re.compile(
    r"gh\s+label\s+create\s+(?:\"([^\"$`\\]+)\"|'([^'$`\\]+)'|([A-Za-z0-9_.:-]+))"
)


def _capture_label(match: re.Match[str]) -> str | None:
    label = next((group for group in match.groups() if group), None)
    if label is None or "$" in label:
        return None
    return label


def static_issue_labels(script: str) -> set[str]:
    """Return literal labels applied by gh issue create calls in a shell hook."""
    labels: set[str] = set()
    for match in _STATIC_LABEL_RE.finditer(script):
        label = _capture_label(match)
        if label:
            labels.add(label)
    return labels


def created_labels(script: str) -> set[str]:
    """Return literal labels created or refreshed by gh label create commands."""
    labels: set[str] = set()
    for match in _LABEL_CREATE_RE.finditer(script):
        label = _capture_label(match)
        if label:
            labels.add(label)
    return labels


def _looks_like_generated_finding_hook(content: str) -> bool:
    normalized = content.lower()
    has_generated_marker = "theforge" in normalized and "post_run hook" in normalized
    return has_generated_marker and "gh issue create" in normalized and "forge-finding" in content


def post_run_hook_upgrade_warnings(project_root: Path, hook_command: str | None) -> list[str]:
    """Return operator-facing warnings for stale generated post_run hooks."""
    path = resolve_hook_path(project_root, hook_command)
    if path is None or not path.exists():
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    if not _looks_like_generated_finding_hook(content):
        return []

    warnings: list[str] = []
    expected_issue_labels = static_issue_labels(_POST_RUN_SH)
    actual_issue_labels = static_issue_labels(content)
    missing_issue_labels = sorted(expected_issue_labels - actual_issue_labels)
    if missing_issue_labels:
        warnings.append(
            f"{path}: generated finding hook is stale; newly filed forge findings will miss "
            f"{', '.join(missing_issue_labels)} until the hook is updated. Update the hook "
            "from the current template by moving the existing file aside and running "
            "`forge init-hooks`, or copy the missing label lines from src/theforge/cli/hooks.py."
        )

    expected_created_labels = created_labels(_POST_RUN_SH)
    actual_created_labels = created_labels(content)
    missing_created_labels = sorted(expected_created_labels - actual_created_labels)
    if missing_created_labels:
        warnings.append(
            f"{path}: generated finding hook does not ensure "
            f"{', '.join(missing_created_labels)} exists before filing findings. Update the "
            "hook from the current template by moving the existing file aside and running "
            "`forge init-hooks`, or copy the missing label setup from src/theforge/cli/hooks.py."
        )
    return warnings


def cmd_init_hooks(args: object) -> int:
    """Scaffold .forge/hooks/post_run.sh and .forge/hooks/README.md."""
    project_root = Path.cwd()
    hooks_dir = project_root / ".forge" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    sh_path = hooks_dir / "post_run.sh"
    readme_path = hooks_dir / "README.md"
    created: list[str] = []
    skipped: list[str] = []

    if sh_path.exists():
        skipped.append(str(sh_path))
        warnings = post_run_hook_upgrade_warnings(project_root, str(sh_path))
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
    else:
        sh_path.write_text(_POST_RUN_SH, encoding="utf-8")
        sh_path.chmod(0o755)
        created.append(str(sh_path))

    if readme_path.exists():
        skipped.append(str(readme_path))
    else:
        readme_path.write_text(_HOOKS_README, encoding="utf-8")
        created.append(str(readme_path))

    for path in created:
        print(f"Created {path}")
    for path in skipped:
        print(f"Skipped (already exists): {path}", file=sys.stderr)

    print(
        "\nAdd a hooks: block to forge.yaml to activate:\n\n"
        "  hooks:\n"
        "    post_run: .forge/hooks/post_run.sh\n"
        "    timeout_seconds: 30\n"
    )
    return 0
