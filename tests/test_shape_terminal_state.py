"""`forge shape` must be able to report that an issue is ready (#2054).

A recommendation that always names a further pipeline stage gives the operator
no way to tell progress from a loop, and an exit code that is non-zero whenever
a proposal exists cannot distinguish a gate-passing issue from a malformed one.
These tests pin the terminal state: gate-passing input produces a "no further
action" recommendation, exit 0, and no downstream command name.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.shape import cmd_shape
from theforge.coordinator.audit_substrate import substrate_path
from theforge.coordinator.shape_audit import emit_shape_event
from theforge.intake.shape_classify import Classification, classify
from theforge.intake.shape_render import evaluate_readiness, next_command
from theforge.shape_check.types import ShapeVerdict

GATE_PASSING_TITLE = "forge shape proposes labels the issue already carries"
GATE_PASSING_BODY = (
    "## Observed behavior\n\n"
    "`forge shape` proposes work the issue has already had done to it.\n\n"
    "## Expected behavior\n\n"
    "A command that proposes changes proposes only the difference.\n\n"
    "## Diagnosis\n\n"
    "- **Observed symptom:** the recommendation always names a further stage.\n"
    "- **Evidence:** issue #2050 printed `Next: forge groom 2050` while passing.\n"
    "- **Confirmed cause:** proposals are built without reading current state.\n"
    "- **Affected code path:** `intake.shape_render.next_command`.\n"
    "- **Fix-success criterion:** a gate-passing issue reports no action needed.\n"
)

SYMPTOM_ONLY_BODY = "## Observed behavior\n\nthe thing broke on every release run.\n"


def _make_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    data: dict[str, object] = {
        "config": str(forge_yaml),
        "issue": None,
        "from_brief": None,
        "from_stdin": False,
        "apply": False,
        "next": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _gh_view(title: str, body: str, labels: list[str]) -> MagicMock:
    return _proc(
        stdout=json.dumps(
            {
                "title": title,
                "body": body,
                "labels": [{"name": name} for name in labels],
            }
        )
    )


# --- readiness evaluation (pure) --------------------------------------------


def test_readiness_true_for_gate_passing_bug():
    proposal = classify(GATE_PASSING_TITLE, GATE_PASSING_BODY, ["bug"])
    readiness = evaluate_readiness(
        proposal, title=GATE_PASSING_TITLE, body=GATE_PASSING_BODY, labels=["bug"]
    )
    assert readiness.verdict is ShapeVerdict.RUNNABLE
    assert readiness.ready is True


def test_readiness_false_for_symptom_only_bug():
    proposal = classify("thing is broken", SYMPTOM_ONLY_BODY, ["bug"])
    readiness = evaluate_readiness(
        proposal, title="thing is broken", body=SYMPTOM_ONLY_BODY, labels=["bug"]
    )
    assert readiness.verdict is ShapeVerdict.NEEDS_DIAGNOSIS
    assert readiness.ready is False


def test_readiness_false_while_a_label_edit_is_still_pending():
    """The gate may pass while the proposal still asks for a label edit."""
    labels = ["bug", "todo:draft"]
    proposal = classify(GATE_PASSING_TITLE, GATE_PASSING_BODY, labels)
    readiness = evaluate_readiness(
        proposal, title=GATE_PASSING_TITLE, body=GATE_PASSING_BODY, labels=labels
    )
    assert proposal.removed_labels == ("todo:draft",)
    assert readiness.verdict is ShapeVerdict.RUNNABLE
    assert readiness.ready is False


def test_readiness_false_for_unresolved_draft():
    proposal = classify("???", "", [])
    assert proposal.classification is Classification.UNRESOLVED
    readiness = evaluate_readiness(proposal, title="???", body="", labels=[])
    assert readiness.ready is False


# --- next_command terminal state --------------------------------------------


def test_next_command_terminal_when_ready():
    proposal = classify(GATE_PASSING_TITLE, GATE_PASSING_BODY, ["bug"])
    text = next_command(proposal, 2050, ready=True)
    assert "no further action is needed" in text
    assert "#2050" in text
    assert "forge groom" not in text
    assert "forge diagnose" not in text


def test_next_command_terminal_without_issue_number():
    proposal = classify(GATE_PASSING_TITLE, GATE_PASSING_BODY, ["bug"])
    text = next_command(proposal, None, ready=True)
    assert "no further action is needed" in text
    assert "forge groom" not in text


def test_next_command_still_names_a_stage_when_not_ready():
    proposal = classify("thing is broken", SYMPTOM_ONLY_BODY, ["bug"])
    assert next_command(proposal, 42, ready=False) == "Next: forge diagnose 42"


# --- CLI wiring -------------------------------------------------------------


@patch("theforge.cli.shape.subprocess.run")
def test_shape_reports_no_action_needed_and_exits_zero(mock_run, tmp_path, capsys):
    mock_run.return_value = _gh_view(GATE_PASSING_TITLE, GATE_PASSING_BODY, ["bug"])
    rc = cmd_shape(_make_args(tmp_path, issue=2050))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no further action is needed" in out
    assert "Add labels:" not in out
    assert "forge groom" not in out


@patch("theforge.cli.shape.subprocess.run")
def test_shape_next_flag_reports_terminal_state(mock_run, tmp_path, capsys):
    mock_run.return_value = _gh_view(GATE_PASSING_TITLE, GATE_PASSING_BODY, ["bug"])
    rc = cmd_shape(_make_args(tmp_path, issue=2050, next=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no further action is needed" in out
    assert "forge groom" not in out


@patch("theforge.cli.shape.subprocess.run")
def test_shape_exits_nonzero_and_names_a_stage_when_work_remains(mock_run, tmp_path, capsys):
    mock_run.return_value = _gh_view("thing is broken", SYMPTOM_ONLY_BODY, ["bug"])
    rc = cmd_shape(_make_args(tmp_path, issue=77))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Next: forge diagnose 77" in out


@patch("theforge.cli.shape.subprocess.run")
def test_apply_on_gate_passing_issue_makes_no_edit(mock_run, tmp_path, capsys):
    mock_run.return_value = _gh_view(GATE_PASSING_TITLE, GATE_PASSING_BODY, ["bug"])
    rc = cmd_shape(_make_args(tmp_path, issue=2050, apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No changes to apply" in out
    # Only the gh issue view — no gh issue edit of any kind.
    assert mock_run.call_count == 1
    assert mock_run.call_args.args[0][:3] == ["gh", "issue", "view"]


@patch("theforge.cli.shape.subprocess.run")
def test_apply_skips_body_edit_when_only_labels_change(mock_run, tmp_path, capsys):
    """A body write that replaces the body with its own content is not a change."""
    labels = ["bug", "todo:draft"]
    mock_run.side_effect = [
        _gh_view(GATE_PASSING_TITLE, GATE_PASSING_BODY, labels),
        _proc(stdout="ok"),
    ]
    rc = cmd_shape(_make_args(tmp_path, issue=2050, apply=True))
    assert rc == 0
    argvs = [call.args[0] for call in mock_run.call_args_list]
    assert argvs[1][-2:] == ["--remove-label", "todo:draft"]
    assert not any("--body-file" in argv for argv in argvs)


@patch("theforge.cli.shape.subprocess.run")
def test_gate_failing_issue_with_no_proposal_still_exits_nonzero(mock_run, tmp_path, capsys):
    """No proposed edits is not the same as ready — the gate decides."""
    mock_run.return_value = _gh_view(
        "Epic: intake automation", "roll-up of intake work.", ["epic"]
    )
    rc = cmd_shape(_make_args(tmp_path, issue=900))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Add labels:" not in out
    assert "split into child stories" in out
    assert _gate_verdicts(tmp_path) == ["needs_operator_action"]


@patch("theforge.cli.shape.subprocess.run")
def test_apply_reports_honestly_when_no_edit_differs(mock_run, tmp_path, capsys):
    mock_run.return_value = _gh_view(
        "Epic: intake automation", "roll-up of intake work.", ["epic"]
    )
    rc = cmd_shape(_make_args(tmp_path, issue=900, apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing to apply" in out
    assert "Applied proposal" not in out
    assert mock_run.call_count == 1
    # An invocation that mutated nothing must not be audited as a mutation.
    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        assert conn.execute("SELECT apply_mutated FROM shape_events").fetchone()[0] == 0
    finally:
        conn.close()


# --- audit provenance -------------------------------------------------------


def _gate_verdicts(tmp_path: Path) -> list[str | None]:
    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        return [row[0] for row in conn.execute("SELECT gate_verdict FROM shape_events")]
    finally:
        conn.close()


@patch("theforge.cli.shape.subprocess.run")
def test_audit_records_observed_gate_verdict(mock_run, tmp_path, capsys):
    mock_run.return_value = _gh_view(GATE_PASSING_TITLE, GATE_PASSING_BODY, ["bug"])
    cmd_shape(_make_args(tmp_path, issue=2050))
    capsys.readouterr()
    assert _gate_verdicts(tmp_path) == ["runnable"]


@patch("theforge.cli.shape.subprocess.run")
def test_audit_records_no_verdict_when_issue_never_loaded(mock_run, tmp_path, capsys):
    mock_run.return_value = _proc(returncode=1, stderr="no such issue")
    assert cmd_shape(_make_args(tmp_path, issue=999999)) == 1
    capsys.readouterr()
    assert _gate_verdicts(tmp_path) == [None]


def test_emit_shape_event_migrates_legacy_substrate(tmp_path):
    """A substrate created before gate_verdict existed still accepts events."""
    db = substrate_path(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE shape_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, emitted_at TEXT NOT NULL, "
            "issue_number INTEGER, input_source TEXT NOT NULL, "
            "classification TEXT NOT NULL, confidence TEXT NOT NULL, "
            "ambiguity_question_count INTEGER NOT NULL DEFAULT 0, "
            "apply_mutated INTEGER NOT NULL DEFAULT 0, diagnosis_state TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    emit_shape_event(
        tmp_path,
        issue_number=2050,
        input_source="issue",
        classification="bug",
        confidence="high",
        ambiguity_question_count=0,
        apply_mutated=False,
        gate_verdict="runnable",
    )
    assert _gate_verdicts(tmp_path) == ["runnable"]
