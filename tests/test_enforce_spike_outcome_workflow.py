"""End-to-end behavior test for the spike-outcome enforcement workflow.

GitHub has no synchronous pre-close hook, so the one close path TheForge cannot
refuse in advance is a human pressing "Close issue" in the web UI. This
workflow is that backstop: it re-asks the guard after the close and restores
the open state when the close was not a legal exit (#2600).

Like the other workflow tests, this extracts the workflow's *actual* shell step
and runs it under ``bash`` with ``gh`` stubbed at the process boundary, so the
shipped YAML is what runs. The guard itself is real.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.gh_stub import install_stubs, stub_env, workflow_step

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "enforce-spike-outcome.yml"
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to run the workflow step"
)


def _run_step(tmp_path: Path, fake_env: dict[str, str]) -> dict[str, str]:
    doc = yaml.safe_load(WORKFLOW.read_text())
    script = workflow_step(doc, "reopen-spike-closed-without-outcome")

    bindir, logs = install_stubs(tmp_path)
    env = {
        **stub_env(bindir, logs, tmp_path),
        "REPO": "fuzzypete/theforge",
        "ISSUE_NUMBER": "2348",
        **fake_env,
    }
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"step failed: {proc.stderr}"
    return {"stdout": proc.stdout, **{name: p.read_text() for name, p in logs.items()}}


def test_manual_close_of_an_outcomeless_spike_is_reversed(tmp_path):
    """AC: the rule is enforced, not offered as guidance a closing operator may follow."""
    result = _run_step(
        tmp_path,
        {
            "FAKE_STATE_2348": "CLOSED",
            "FAKE_LABELS_2348": "spike",
            "FAKE_BODY_2348": "Does the structural decay observer earn its keep?",
        },
    )
    assert "2348" in result["reopen"]
    assert "records no outcome" in result["comment"]


def test_spike_with_a_do_not_proceed_decision_stays_closed(tmp_path):
    """AC: 'do not do this' is a complete, successful outcome — the close stands."""
    result = _run_step(
        tmp_path,
        {
            "FAKE_STATE_2348": "CLOSED",
            "FAKE_LABELS_2348": "spike",
            "FAKE_BODY_2348": (
                "<!-- forge-spike-outcome-v1\noutcome: do_not_proceed\n"
                "reason: the POC declines to be used and the signal is unreachable\n-->"
            ),
        },
    )
    assert result["reopen"].strip() == ""
    assert result["comment"].strip() == ""
    assert "leaving it closed" in result["stdout"]


def test_outcome_recorded_in_a_comment_counts(tmp_path):
    """An outcome recorded as a comment on the spike is as good as one in the body."""
    result = _run_step(
        tmp_path,
        {
            "FAKE_STATE_2348": "CLOSED",
            "FAKE_LABELS_2348": "spike",
            "FAKE_BODY_2348": "Does the observer earn its keep?",
            "FAKE_COMMENT_2348": (
                "Not yet.\n\n<!-- forge-spike-outcome-v1\noutcome: follow_up\n"
                "follow-up: #2599\n-->"
            ),
            "FAKE_STATE_2599": "OPEN",
            "FAKE_LABELS_2599": "enhancement",
        },
    )
    assert result["reopen"].strip() == ""
    assert "leaving it closed" in result["stdout"]


def test_follow_on_that_is_still_a_draft_does_not_count(tmp_path):
    """AC: the follow-on must be visible where other work is visible, not a draft."""
    result = _run_step(
        tmp_path,
        {
            "FAKE_STATE_2348": "CLOSED",
            "FAKE_LABELS_2348": "spike",
            "FAKE_BODY_2348": (
                "<!-- forge-spike-outcome-v1\noutcome: follow_up\nfollow-up: #2599\n-->"
            ),
            "FAKE_STATE_2599": "OPEN",
            "FAKE_LABELS_2599": "todo:draft",
        },
    )
    assert "2348" in result["reopen"]
    assert "todo:draft" in result["comment"]


def test_workflow_is_label_gated_on_the_spike_label(tmp_path):
    """An ordinary issue closing must not spin up a runner, so the job is gated."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    job = doc["jobs"]["reopen-spike-closed-without-outcome"]
    assert "spike" in job["if"]
    assert doc[True] == {"issues": {"types": ["closed"]}}
