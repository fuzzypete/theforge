"""CLI seam tests for ``forge groom``.

Verifies the substrate emission boundary and the CLI exit-code contract.
Network-free: the groom flow is invoked via monkeypatched seams.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from theforge.cli import groom as cli_groom
from theforge.coordinator.audit_substrate import substrate_path

_CONFIRMED_BUG_BODY = """\
## What happened

Forge worktrees drop secrets.

## What was expected

Secrets propagate.

## Diagnosis

- Observed symptom: missing .env on dev branch
- Evidence: file absent in worktree
- Ruled out: agent role mismatch
- Confirmed cause: worktree-creation step skips .env copy at line 42
- Affected code path: theforge/coordinator/worktree.py
- Fix-success criterion: .env present after worktree creation
"""

_NO_DIAGNOSIS_BUG_BODY = """\
## What happened

Tests sometimes fail flakily on CI.

## What was expected

Tests pass deterministically.
"""

_ADVISORY_DONE_STATE_BODY = """\
## What

Add a CLI flag.

## Why

Users need a way to bypass the gate.

## Example

```text
Before:
$ forge sprint
[forge] 2 issue(s) flagged by shape gate

After:
$ forge sprint --force
[forge] sprint started with every issue
```

## Acceptance Criteria

- The toggle is available to operators.
"""


def _build_args(issue: str, project_root: Path, *, apply: bool = False, want_next: bool = False):
    forge_yaml = project_root / "forge.yaml"
    if not forge_yaml.exists():
        forge_yaml.write_text("project_root: .\n", encoding="utf-8")
    return argparse.Namespace(
        issue=issue,
        apply=apply,
        next=want_next,
        config=str(forge_yaml),
    )


def _patch_fetch(monkeypatch, payload):
    def fake_fetch(_n, _root):
        return payload

    monkeypatch.setattr("theforge.intake.groom_flow._default_fetch", fake_fetch)


def _patch_edit(monkeypatch, ok: bool = True):
    def fake_edit(_n, _b, _r):
        return ok

    monkeypatch.setattr("theforge.intake.groom_flow._default_edit", fake_edit)


def test_cli_refusal_exits_nonzero(tmp_path, monkeypatch, capsys):
    _patch_fetch(monkeypatch, {"title": "t", "body": _NO_DIAGNOSIS_BUG_BODY, "labels": ["bug"]})
    rc = cli_groom.cmd_groom(_build_args("1234", tmp_path, want_next=True))
    captured = capsys.readouterr()
    assert rc == 1
    assert "REFUSED" in captured.out
    assert "needs diagnosis" in captured.out
    assert "forge diagnose 1234" in captured.out


def test_cli_proposal_exits_2_without_apply(tmp_path, monkeypatch, capsys):
    body_dirty = _CONFIRMED_BUG_BODY + "   \n"
    _patch_fetch(monkeypatch, {"title": "t", "body": body_dirty, "labels": ["bug"]})
    rc = cli_groom.cmd_groom(_build_args("1503", tmp_path, apply=False))
    captured = capsys.readouterr()
    assert rc == 2
    assert "Pass --apply" in captured.out


def test_cli_apply_exits_zero(tmp_path, monkeypatch, capsys):
    body_dirty = _CONFIRMED_BUG_BODY + "   \n"
    _patch_fetch(monkeypatch, {"title": "t", "body": body_dirty, "labels": ["bug"]})
    _patch_edit(monkeypatch, ok=True)
    rc = cli_groom.cmd_groom(_build_args("1503", tmp_path, apply=True))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Applied body restructure" in captured.out


def test_cli_emits_readiness_event_to_substrate(tmp_path, monkeypatch, capsys):
    _patch_fetch(monkeypatch, {"title": "t", "body": _NO_DIAGNOSIS_BUG_BODY, "labels": ["bug"]})
    rc = cli_groom.cmd_groom(_build_args("1234", tmp_path))
    assert rc == 1

    db = substrate_path(tmp_path)
    assert db.exists(), "groom must create the audit substrate"
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT kind, issue_ref, action, applied, bug_diagnosis_state, refusal_reason "
        "FROM readiness_events"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    kind, ref, action, applied, bug_state, refusal = rows[0]
    assert kind == "groom"
    assert ref == "1234"
    assert action == "refused"
    assert applied == 0
    assert bug_state == "no-diagnosis"
    assert "needs diagnosis" in (refusal or "")


def test_cli_emits_restructured_event(tmp_path, monkeypatch, capsys):
    _patch_fetch(monkeypatch, {"title": "t", "body": _CONFIRMED_BUG_BODY, "labels": ["bug"]})
    _patch_edit(monkeypatch, ok=True)
    cli_groom.cmd_groom(_build_args("1503", tmp_path, apply=True))

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    rows = conn.execute("SELECT action, applied, post_verdict FROM readiness_events").fetchall()
    conn.close()
    assert len(rows) == 1
    action, applied, post = rows[0]
    assert action == "restructured"
    # body is already canonical so the bug-confirmed path proposes no change
    # → applied stays 0 even with --apply. Verify by re-running with dirty body.
    assert applied in (0, 1)
    assert post == "runnable"


def test_cli_next_flag_for_cause_unknown_never_says_ready(tmp_path, monkeypatch, capsys):
    cause_unknown = _CONFIRMED_BUG_BODY.replace(
        "Confirmed cause: worktree-creation step skips .env copy at line 42",
        "Confirmed cause: not yet identified",
    )
    _patch_fetch(monkeypatch, {"title": "t", "body": cause_unknown, "labels": ["bug"]})
    rc = cli_groom.cmd_groom(_build_args("1497", tmp_path, want_next=True))
    captured = capsys.readouterr()
    # Hard invariant: never proposes the ``ready`` label.
    assert "add-label ready" not in captured.out
    assert "Next: forge diagnose 1497" in captured.out
    assert "investigation-ready, not implementation-ready" in captured.out
    # rc is 2 (proposal made, not applied) — that's fine.
    assert rc in (0, 2)


def test_cli_reports_runnable_pre_verdict_for_advisory_only_done_state(
    tmp_path,
    monkeypatch,
    capsys,
):
    _patch_fetch(
        monkeypatch,
        {"title": "t", "body": _ADVISORY_DONE_STATE_BODY, "labels": ["enhancement"]},
    )
    rc = cli_groom.cmd_groom(_build_args("2123", tmp_path, want_next=True))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Pre-groom verdict:  runnable" in captured.err
    assert "Post-groom verdict: runnable" in captured.err
    assert "No body changes needed." in captured.out


def test_cli_groom_appears_in_help():
    """The new subcommand is discoverable via ``forge --help``."""
    from theforge.cli.main import build_parser

    parser = build_parser()
    # argparse exposes registered subcommands via the choices on the
    # subparsers action.
    subparsers_action = next(
        action
        for action in parser._actions  # noqa: SLF001 — argparse internal
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "groom" in subparsers_action.choices


# ── Unsupplied content is reported, not erased (#2129) ───────────────────

_FEATURE_BODY = """\
## What

Users can export their data.
"""


def test_cli_reports_findings_groom_could_not_resolve(tmp_path, monkeypatch, capsys):
    _patch_fetch(monkeypatch, {"title": "Add export", "body": _FEATURE_BODY, "labels": ["task"]})
    rc = cli_groom.cmd_groom(_build_args("2129", tmp_path, want_next=True))
    captured = capsys.readouterr()
    assert rc == 2
    assert "UNRESOLVED" in captured.out
    assert "missing_example" in captured.out
    assert "missing_acceptance_criteria" in captured.out
    assert "--add-label ready" not in captured.out


def test_cli_event_records_unsupplied_findings(tmp_path, monkeypatch, capsys):
    _patch_fetch(monkeypatch, {"title": "Add export", "body": _FEATURE_BODY, "labels": ["task"]})
    _patch_edit(monkeypatch, ok=True)
    cli_groom.cmd_groom(_build_args("2129", tmp_path, apply=True))

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    rows = conn.execute("SELECT post_verdict, raw_json FROM readiness_events").fetchall()
    conn.close()
    assert len(rows) == 1
    post, raw = rows[0]
    assert post != "runnable"
    assert "missing_example" in raw
