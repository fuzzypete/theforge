"""End-to-end behavior test for the close-story-on-merge workflow.

Every issue a merged PR references closes, whatever its labels. This test
extracts the workflow's *actual* shell step and runs it under ``bash`` with the
``gh`` CLI stubbed at the process boundary, verifying that closure does not
depend on the `bug` label and that an already-closed issue is left alone.

Because the script is read straight out of the YAML, the test stays in
lockstep with the shipped workflow.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.gh_stub import install_stubs, stub_env, workflow_step

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "close-on-merge.yml"


def _run_step(tmp_path: Path, pr_body: str, fake_env: dict[str, str]) -> dict[str, str]:
    """Run the workflow's shell step with a stubbed gh; return output/log contents."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    script = workflow_step(doc, "close-referenced-issues")

    bindir, logs = install_stubs(tmp_path)
    env = {
        **stub_env(bindir, logs, tmp_path),
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


SPIKE_FOLLOW_UP_BODY = """## Spike trigger condition

- **What must be true:** the observer's ranking beats the naive baseline.
- **How to know:** the comparison hook reports its trust threshold met.
"""


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash required to run the workflow step"
)


def test_bug_issue_closes_on_merge(tmp_path):
    """A `bug`-labeled issue closes on merge like any other (#2067 reverted)."""
    result = _run_step(
        tmp_path,
        "Fixes #2047",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "bug"},
    )
    assert "2047" in result["close"]
    assert "Closed by merged PR #2062" in result["close"]
    assert result["edit"].strip() == "", "no label edit: the issue simply closes"


def test_non_bug_issue_closes_on_merge(tmp_path):
    """A story/enhancement issue closes on merge; label plays no part in the decision."""
    result = _run_step(
        tmp_path,
        "Closes #100",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "enhancement"},
    )
    assert result["edit"].strip() == ""
    assert "100" in result["close"]
    assert "Closed by merged PR #2062" in result["close"]


def test_already_closed_issue_is_skipped(tmp_path):
    """An already-closed issue is left alone regardless of label (idempotent)."""
    result = _run_step(
        tmp_path,
        "Fixes #2047",
        {"FAKE_STATE": "CLOSED", "FAKE_LABELS": "bug"},
    )
    assert result["close"].strip() == ""
    assert result["edit"].strip() == ""
    assert "already closed" in result["stdout"]


def test_spike_without_a_recorded_outcome_is_not_closed(tmp_path):
    """AC: a merged PR is not a legal exit from a spike, so the issue stays open."""
    result = _run_step(
        tmp_path,
        "Closes #2348",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "spike", "FAKE_BODY": "A design question."},
    )
    assert result["close"].strip() == "", "a spike with no outcome must stay open"
    assert "records no outcome" in result["comment"]
    assert "Not closing #2348" in result["stdout"]


def test_spike_with_do_not_proceed_closes(tmp_path):
    """AC: 'do not proceed' is a complete outcome, recorded as one — the spike closes."""
    body = (
        "A design question.\n\n"
        "<!-- forge-spike-outcome-v1\n"
        "outcome: do_not_proceed\n"
        "reason: the trust threshold is unreachable with the available signal\n"
        "-->\n"
    )
    result = _run_step(
        tmp_path,
        "Closes #2348",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "spike", "FAKE_BODY": body},
    )
    assert "2348" in result["close"]
    assert result["comment"].strip() == ""


def test_spike_with_conditional_follow_up_closes_when_the_condition_is_carried(tmp_path):
    """AC: a conditional answer closes when the follow-on artifact carries the condition."""
    body = "<!-- forge-spike-outcome-v1\noutcome: conditional_follow_up\nfollow-up: #2599\n-->\n"
    result = _run_step(
        tmp_path,
        "Closes #2348",
        {
            "FAKE_LABELS_2348": "spike",
            "FAKE_BODY_2348": body,
            "FAKE_STATE_2599": "OPEN",
            "FAKE_LABELS_2599": "enhancement",
            "FAKE_BODY_2599": SPIKE_FOLLOW_UP_BODY,
        },
    )
    assert "2348" in result["close"]


def test_spike_conditional_follow_up_without_the_condition_is_refused(tmp_path):
    """AC: the condition must be carried by the follow-on, not by the closed spike's prose."""
    body = (
        "The condition is that the trust threshold is met.\n\n"
        "<!-- forge-spike-outcome-v1\noutcome: conditional_follow_up\nfollow-up: #2599\n-->\n"
    )
    result = _run_step(
        tmp_path,
        "Closes #2348",
        {
            "FAKE_LABELS_2348": "spike",
            "FAKE_BODY_2348": body,
            "FAKE_STATE_2599": "OPEN",
            "FAKE_LABELS_2599": "enhancement",
            "FAKE_BODY_2599": "Adopt the observer.",
        },
    )
    assert result["close"].strip() == ""
    assert "Spike trigger condition" in result["comment"]


def test_no_label_is_created(tmp_path):
    """Closure no longer depends on any label, so none is created (#2067 reverted)."""
    result = _run_step(
        tmp_path,
        "Fixes #2047",
        {"FAKE_STATE": "OPEN", "FAKE_LABELS": "bug"},
    )
    assert result["label"].strip() == ""
