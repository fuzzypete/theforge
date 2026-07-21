"""End-to-end behavior test for the close-epic-on-last-subissue workflow.

The acceptance criterion "close the last sub-issue of a test epic and observe
the parent close" cannot be exercised as a real GitHub Actions run until the
workflow lands on the default branch. This test provides the equivalent,
committed verification: it extracts the workflow's *actual* shell step and runs
it under ``bash`` with the ``gh`` CLI stubbed at the process boundary, driving
the same command sequence the Action performs at runtime.

Because the script is read straight out of the YAML, the test stays in lockstep
with the shipped workflow — editing the workflow logic re-runs the edited logic
here. The external ``gh`` boundary is fully stubbed, so the test needs no
network, no GitHub token, and no provider SDKs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "close-epic-on-last-subissue.yml"
)

# A stub `gh` that answers the four call shapes the workflow makes, driven by
# FAKE_* env vars. `gh api graphql` -> parent number; `gh api .../sub_issues`
# -> all numbers, or just the open ones when the --jq carries a `select`;
# `gh issue view` -> parent state; `gh issue close` -> recorded to $CLOSE_LOG.
GH_STUB = r"""#!/usr/bin/env bash
set -eu
case "$1" in
  api)
    if [ "$2" = "graphql" ]; then
      printf '%s' "${FAKE_PARENT:-}"
      exit 0
    fi
    for a in "$@"; do
      case "$a" in
        *select*) printf '%s\n' ${FAKE_SUBS_OPEN:-}; exit 0 ;;
      esac
    done
    printf '%s\n' ${FAKE_SUBS_ALL:-}
    exit 0
    ;;
  issue)
    case "$2" in
      view) printf '%s' "${FAKE_PARENT_STATE:-OPEN}"; exit 0 ;;
      close) printf '%s\n' "$*" >> "$CLOSE_LOG"; exit 0 ;;
    esac
    ;;
esac
echo "unexpected gh call: $*" >&2
exit 1
"""


def _run_step(tmp_path: Path, fake_env: dict[str, str]) -> tuple[str, str]:
    """Run the workflow's shell step with a stubbed gh; return (stdout, close_log)."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = doc["jobs"]["close-parent-epic-if-complete"]["steps"]
    script = steps[0]["run"]

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(0o755)

    close_log = tmp_path / "close.log"
    close_log.write_text("")

    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "CLOSE_LOG": str(close_log),
        # Context the workflow reads from `env:` (github.* at runtime).
        "GH_TOKEN": "stub",
        "REPO": "fuzzypete/theforge",
        "OWNER": "fuzzypete",
        "REPO_NAME": "theforge",
        "ISSUE_NUMBER": "1823",
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
    return proc.stdout, close_log.read_text()


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to run the workflow step"
)


def test_last_subissue_closed_closes_parent_with_summary(tmp_path):
    """AC: last open sub-issue closing auto-closes the parent with a summary comment."""
    stdout, close_log = _run_step(
        tmp_path,
        {
            "FAKE_PARENT": "1725",
            "FAKE_PARENT_STATE": "OPEN",
            "FAKE_SUBS_ALL": "1801 1802 1823",
            "FAKE_SUBS_OPEN": "",
        },
    )
    assert "auto-closing" in stdout
    # The parent epic was closed...
    assert close_log.strip(), "expected `gh issue close` to be invoked"
    assert "1725" in close_log
    # ...with a summary comment listing every sub-issue.
    assert "Sub-issues: #1801, #1802, #1823" in close_log
    assert "its last open sub-issue" in close_log


def test_open_subissue_leaves_parent_open(tmp_path):
    """AC: an epic with any sub-issue still open is left open."""
    stdout, close_log = _run_step(
        tmp_path,
        {
            "FAKE_PARENT": "1725",
            "FAKE_PARENT_STATE": "OPEN",
            "FAKE_SUBS_ALL": "1801 1802 1823",
            "FAKE_SUBS_OPEN": "1802",
        },
    )
    assert "still has 1 open sub-issue" in stdout
    assert close_log.strip() == "", "parent must not be closed while a sub-issue is open"


def test_no_parent_is_noop(tmp_path):
    """A closed issue with no parent epic is a no-op."""
    stdout, close_log = _run_step(tmp_path, {"FAKE_PARENT": ""})
    assert "no parent epic" in stdout
    assert close_log.strip() == ""


def test_already_closed_parent_is_idempotent(tmp_path):
    """A parent that is already closed is skipped (idempotent)."""
    stdout, close_log = _run_step(
        tmp_path,
        {
            "FAKE_PARENT": "1725",
            "FAKE_PARENT_STATE": "CLOSED",
            "FAKE_SUBS_ALL": "1801 1802 1823",
            "FAKE_SUBS_OPEN": "",
        },
    )
    assert "is CLOSED; skipping" in stdout
    assert close_log.strip() == ""
