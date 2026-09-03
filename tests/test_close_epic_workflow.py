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

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.gh_stub import install_stubs, stub_env, workflow_step

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "close-epic-on-last-subissue.yml"
)


def _run_step(tmp_path: Path, fake_env: dict[str, str]) -> tuple[str, str]:
    """Run the workflow's shell step with a stubbed gh; return (stdout, close_log)."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    script = workflow_step(doc, "close-parent-epic-if-complete")

    bindir, logs = install_stubs(tmp_path)
    env = {
        **stub_env(bindir, logs, tmp_path),
        # Context the workflow reads from `env:` (github.* at runtime).
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
    return proc.stdout, logs["close"].read_text()


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to run the workflow step"
)


def test_last_subissue_closed_closes_parent_with_summary(tmp_path):
    """AC: last open sub-issue closing auto-closes the parent with a summary comment."""
    stdout, close_log = _run_step(
        tmp_path,
        {
            "FAKE_PARENT": "1725",
            "FAKE_STATE_1725": "OPEN",
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
            "FAKE_STATE_1725": "OPEN",
            "FAKE_SUBS_ALL": "1801 1802 1823",
            "FAKE_SUBS_OPEN": "1802",
        },
    )
    assert "still has 1 open sub-issue" in stdout
    assert close_log.strip() == "", "parent must not be closed while a sub-issue is open"


def test_spike_parent_without_an_outcome_is_not_closed(tmp_path):
    """This workflow never checks the parent is an epic, so a spike parent must be guarded.

    All sub-issues closed is not one of a spike's two legal exits (#2600).
    """
    stdout, close_log = _run_step(
        tmp_path,
        {
            "FAKE_PARENT": "1725",
            "FAKE_STATE_1725": "OPEN",
            "FAKE_LABELS_1725": "spike",
            "FAKE_SUBS_ALL": "1801 1802 1823",
            "FAKE_SUBS_OPEN": "",
        },
    )
    assert "Not closing parent #1725" in stdout
    assert close_log.strip() == ""


def test_spike_parent_with_a_recorded_outcome_closes(tmp_path):
    """A spike parent that recorded a do-not-proceed decision closes like any other."""
    stdout, close_log = _run_step(
        tmp_path,
        {
            "FAKE_PARENT": "1725",
            "FAKE_STATE_1725": "OPEN",
            "FAKE_LABELS_1725": "spike",
            "FAKE_BODY_1725": (
                "<!-- forge-spike-outcome-v1\n"
                "outcome: do_not_proceed\n"
                "reason: the approach does not pay for its complexity\n-->"
            ),
            "FAKE_SUBS_ALL": "1801 1802 1823",
            "FAKE_SUBS_OPEN": "",
        },
    )
    assert "auto-closing" in stdout
    assert "1725" in close_log


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
            "FAKE_STATE_1725": "CLOSED",
            "FAKE_SUBS_ALL": "1801 1802 1823",
            "FAKE_SUBS_OPEN": "",
        },
    )
    assert "is CLOSED; skipping" in stdout
    assert close_log.strip() == ""
