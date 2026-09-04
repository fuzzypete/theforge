from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge.baseline_repair import (
    BaselineRepairError,
    load_baseline_repair_evidence,
    render_issue_body,
    render_issue_title,
)


def _write_sprint_audit(
    tmp_path: Path,
    *,
    run_id: str = "abc123",
    failure_reproduced: bool | None = True,
    worktree_exists: bool = True,
    evidence_exists: bool = True,
) -> tuple[Path, Path, Path]:
    worktree = tmp_path / ".forge" / "baseline-repro" / "worktree"
    if worktree_exists:
        worktree.mkdir(parents=True)

    evidence_path = tmp_path / ".forge" / "logs" / "Demo Sprint" / "run-abc123-baseline-gate.txt"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    if evidence_exists:
        evidence_path.write_text(
            "\n".join(
                [
                    "# baseline gate FAIL on merge base abcdef1234567890",
                    "# gate command: pytest -q",
                    "# exit code: 1",
                    f"# worktree: {worktree}",
                    "",
                    "EARLY-MARKER",
                    "FAILED tests/test_storage.py::test_dates",
                    "AssertionError: dates drifted",
                ]
            ),
            encoding="utf-8",
        )

    audit_path = tmp_path / ".forge" / "audits" / f"run-{run_id}-sprint-audit.yaml"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        yaml.safe_dump(
            {
                "sprint": {
                    "name": "Demo Sprint",
                    "stopped_reason": "broken_baseline",
                },
                "baseline_check": {
                    "passed": False,
                    "failure_reproduced": failure_reproduced,
                    "merge_base": "abcdef1234567890",
                    "command": "pytest -q",
                    "validation_profile": "merge",
                    "validation_authority": "authoritative",
                    "output_tail": "tail only",
                    "worktree": str(worktree),
                    "evidence_path": str(evidence_path),
                    "failing_targets": ["tests/test_storage.py::test_dates"],
                    "failing_target_extraction": {
                        "source": "custom_pattern",
                        "format_recognized": True,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return audit_path, worktree, evidence_path


def test_load_and_render_reproduced_baseline_failure(tmp_path: Path) -> None:
    audit_path, worktree, evidence_path = _write_sprint_audit(tmp_path)

    evidence = load_baseline_repair_evidence(audit_path)

    assert evidence.sprint_run_id == "abc123"
    assert evidence.failing_targets == ("tests/test_storage.py::test_dates",)
    assert evidence.evidence_text is not None

    title = render_issue_title(evidence)
    body = render_issue_body(evidence, excerpt_chars=180)

    assert title == "Fix reproduced baseline gate failure in tests/test_storage.py::test_dates"
    assert str(audit_path.resolve()) in body
    assert str(evidence_path) in body
    assert str(worktree) in body
    assert "source=custom_pattern, format_recognized=yes" in body
    assert "tests/test_storage.py::test_dates" in body
    assert "EARLY-MARKER" in body
    assert "Confirmed cause:** not yet identified" in body


def test_load_rejects_non_reproduced_baseline_failure_with_explicit_reason(
    tmp_path: Path,
) -> None:
    audit_path, _worktree, _evidence_path = _write_sprint_audit(
        tmp_path,
        failure_reproduced=None,
    )

    with pytest.raises(BaselineRepairError, match="failure_reproduced is missing"):
        load_baseline_repair_evidence(audit_path)


def test_load_rejects_missing_preserved_worktree(tmp_path: Path) -> None:
    audit_path, worktree, _evidence_path = _write_sprint_audit(
        tmp_path,
        worktree_exists=False,
    )

    with pytest.raises(BaselineRepairError, match=str(worktree)):
        load_baseline_repair_evidence(audit_path)
