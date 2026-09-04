from __future__ import annotations

import datetime
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from theforge.baseline_repair import (
    BaselineRepairError,
    load_baseline_repair_evidence,
    render_issue_body,
    render_issue_title,
)
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.sprint.audit import _write_sprint_audit as write_sprint_audit_record
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import _run_baseline_gate
from theforge.sprint.sources import FileSource


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


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(cwd: Path, message: str) -> str:
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def _make_gate_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=replace(
            DEFAULT_VALIDATION,
            gate_command=(
                'python -c "import sys; '
                "print('FAILED tests/test_storage.py::test_dates'); "
                "print('AssertionError: dates drifted'); sys.exit(1)\""
            ),
            failed_test_pattern=r"^FAILED (?P<test>\S+)",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_resolved_sprint(tmp_path: Path) -> ResolvedSprint:
    story_file = tmp_path / "story.md"
    story_file.write_text(
        "---\nname: My Story\nslug: my-story\n---\n# Content\n",
        encoding="utf-8",
    )
    source = FileSource()
    task = source.fetch(str(story_file.relative_to(tmp_path)), tmp_path)
    return ResolvedSprint(
        name="Demo Sprint",
        budget_usd=10.0,
        stories=[(task, source, "story.md")],
        max_parallel=1,
    )


def _init_repo_for_baseline_gate(tmp_path: Path) -> tuple[ForgeConfig, ResolvedSprint, str]:
    config = _make_gate_config(tmp_path)
    resolved = _make_resolved_sprint(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    base_commit = _commit_all(tmp_path, "base")
    _git(tmp_path, "checkout", "-b", "feat/test")
    (tmp_path / "tracked.txt").write_text("feature\n", encoding="utf-8")
    _commit_all(tmp_path, "feature")
    return config, resolved, base_commit


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


def test_load_repair_evidence_from_writer_produced_broken_baseline_audit(
    tmp_path: Path,
) -> None:
    config, resolved, base_commit = _init_repo_for_baseline_gate(tmp_path)
    baseline = _run_baseline_gate(config, resolved, run_id="abc123")
    assert baseline["passed"] is False

    write_sprint_audit_record(
        manifest=SimpleNamespace(
            name=resolved.name,
            budget_usd=resolved.budget_usd,
            max_parallel=resolved.max_parallel,
            baseline_gate=baseline,
        ),
        result=SimpleNamespace(
            results=[],
            total_cost_usd=0.0,
            specs_total=0,
            specs_succeeded=0,
            specs_failed=0,
            specs_skipped=0,
            stopped_reason="broken_baseline",
        ),
        canonical_refs=[],
        started_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 1, 1, 0, 0, 1, tzinfo=datetime.timezone.utc),
        duration=1.0,
        project_root=tmp_path,
        run_id="abc123",
    )

    evidence = load_baseline_repair_evidence(
        tmp_path / ".forge" / "audits" / "run-abc123-sprint-audit.yaml"
    )

    assert evidence.sprint_run_id == "abc123"
    assert evidence.merge_base == base_commit
    assert evidence.failing_targets == ("tests/test_storage.py::test_dates",)
    assert evidence.failing_target_source == "custom_pattern"
    assert evidence.failing_target_format_recognized is True
    assert evidence.evidence_path is not None
    assert evidence.evidence_text is not None
    assert "AssertionError: dates drifted" in evidence.evidence_text
    assert evidence.worktree.is_dir()


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
