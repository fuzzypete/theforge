"""End-to-end behavior test for the close-story-on-merge workflow.

Closing an issue asserts its reported problem no longer occurs. A merge event
only establishes that a change shipped, not that a reported symptom stopped.
This test extracts the workflow's *actual* shell step and runs it under
``bash`` with the ``gh`` CLI stubbed at the process boundary, verifying that a
`bug`-labeled issue (whose resolution is defined by an observation) is left
open with a `verify-symptom` label instead of closed, while a non-bug issue
still closes on merge as before.

Because the script is read straight out of the YAML, the test stays in
lockstep with the shipped workflow.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "close-on-merge.yml"

# A stub `gh` that answers the call shapes the workflow makes, driven by
# FAKE_* env vars:
#   gh issue view <n> --json state,labels  -> $FAKE_STATE / $FAKE_LABELS (comma-separated)
#   gh label create                        -> recorded to $LABEL_LOG
#   gh issue close <n> ...                 -> recorded to $CLOSE_LOG
#   gh issue edit <n> --add-label ...      -> recorded to $EDIT_LOG
#   gh issue comment <n> ...               -> recorded to $COMMENT_LOG
GH_STUB = r"""#!/usr/bin/env bash
set -eu
case "$1" in
  label)
    printf '%s\n' "$*" >> "$LABEL_LOG"
    exit 0
    ;;
  issue)
    case "$2" in
      view)
        labels="${FAKE_LABELS:-}"
        json='{"state":"'"${FAKE_STATE:-OPEN}"'","labels":['
        first=1
        IFS=',' read -ra parts <<< "$labels"
        for l in "${parts[@]}"; do
          [ -z "$l" ] && continue
          if [ "$first" = 1 ]; then first=0; else json="$json,"; fi
          json="$json{\"name\":\"$l\"}"
        done
        json="$json]}"
        printf '%s' "$json"
        exit 0
        ;;
      close)
        printf '%s\n' "$*" >> "$CLOSE_LOG"
        exit 0
        ;;
      edit)
        printf '%s\n' "$*" >> "$EDIT_LOG"
        exit 0
        ;;
      comment)
        printf '%s\n' "$*" >> "$COMMENT_LOG"
        exit 0
        ;;
    esac
    ;;
esac
echo "unexpected gh call: $*" >&2
exit 1
"""


def _run_step(tmp_path: Path, pr_body: str, fake_env: dict[str, str]) -> dict[str, str]:
    """Run the workflow's shell step with a stubbed gh; return output/log contents."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = doc["jobs"]["close-referenced-issues"]["steps"]
    script = steps[0]["run"]

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)

    logs = {name: tmp_path / f"{name}.log" for name in ("label", "close", "edit", "comment")}
    for path in logs.values():
        path.write_text("")

    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "LABEL_LOG": str(logs["label"]),
        "CLOSE_LOG": str(logs["close"]),
        "EDIT_LOG": str(logs["edit"]),
        "COMMENT_LOG": str(logs["comment"]),
        "GH_TOKEN": "stub",
        "REPO": "fuzzypete/theforge",
        "PR_NUMBER": "2062",
        "PR_BODY": pr_body,
        **fake_env,
    }

    proc = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"step failed: {proc.stderr}"
    return {"stdout": proc.stdout, **{name: p.read_text() for name, p in logs.items()}}


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to run the workflow step"
)


def test_bug_issue_is_left_open_pending_symptom_verification(tmp_path):
    """AC: a merged PR fixing a bug issue does not close it before the symptom is observed."""
    result = _run_step(
        tmp_path,
        "Fixes #2047",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "bug"},
    )
    assert result["close"].strip() == "", "a bug issue must not be closed on merge alone"
    assert "2047" in result["edit"]
    assert "verify-symptom" in result["edit"]
    assert "2047" in result["comment"]
    assert "2062" in result["comment"]
    assert "pending symptom verification" in result["stdout"]


def test_non_bug_issue_still_closes_on_merge(tmp_path):
    """A story/enhancement issue (no `bug` label) keeps closing on merge as before."""
    result = _run_step(
        tmp_path,
        "Closes #100",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "enhancement"},
    )
    assert result["edit"].strip() == ""
    assert "100" in result["close"]
    assert "Closed by merged PR #2062" in result["close"]


def test_already_closed_bug_issue_is_skipped(tmp_path):
    """An already-closed issue is left alone regardless of label (idempotent)."""
    result = _run_step(
        tmp_path,
        "Fixes #2047",
        {"FAKE_STATE": "CLOSED", "FAKE_LABELS": "bug"},
    )
    assert result["close"].strip() == ""
    assert result["edit"].strip() == ""
    assert "already closed" in result["stdout"]


def test_label_create_is_invoked_idempotently(tmp_path):
    """The verify-symptom label is (re)created before any bug-issue handling."""
    result = _run_step(
        tmp_path,
        "Fixes #2047",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "bug"},
    )
    assert "verify-symptom" in result["label"]
