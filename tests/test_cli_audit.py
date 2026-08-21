"""Tests for the `forge audit` CLI renderer."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from theforge.cli.audit import cmd_audit


def test_cmd_audit_renders_validation_worktree_state(tmp_path: Path, capsys) -> None:
    audit_path = tmp_path / "audit.yaml"
    audit_path.write_text(
        yaml.safe_dump(
            {
                "task": {"name": "Issue 2618"},
                "outcome": {"success": True, "final_phase": "DONE", "message": "ok"},
                "iterations": {
                    "validation_runs": [
                        {
                            "profile": "complete",
                            "authority": "merge",
                            "result": "PASS",
                            "command": "make gate",
                            "worktree_state": {
                                "untracked": ["scratch.txt"],
                                "ignored": ["build/cache.json"],
                            },
                        }
                    ]
                },
                "cost": {},
                "timing": {},
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )

    rc = cmd_audit(SimpleNamespace(file=str(audit_path)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Validation: complete (merge authority) → PASS" in out
    assert "command: make gate" in out
    assert "worktree_state: untracked=1 (scratch.txt); ignored=1 (build/cache.json)" in out
