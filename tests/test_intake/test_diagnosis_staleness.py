"""Tests for diagnosis-baseline staleness detection.

Covers the seam where ``forge groom`` consults the baseline metadata that
``forge diagnose`` recorded on a bug issue body. Verifies the spec
invariants: confirmed-cause + stale → REFUSED; cause-unknown + stale stays
investigation-ready with a notice; ``--confirm-diagnosis-current`` overrides
the refusal and lands an audit event.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from theforge.coordinator.audit_substrate import (
    create_or_open,
    iter_readiness_events,
    record_readiness_event,
)
from theforge.diagnose_types import (
    DiagnosisArtifact,
    Hypothesis,
    InspectedFile,
    parse_baseline_metadata,
    render_artifact_markdown,
)
from theforge.intake.diagnosis_staleness import (
    StalenessState,
    evaluate_staleness,
)
from theforge.intake.groom_flow import (
    BugDiagnosisState,
    GroomAction,
    run_groom,
)


def _git_init(repo: Path) -> None:
    """Initialize a tiny repo with one commit, returning the project root."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)


def _commit_file(repo: Path, rel: str, content: str, msg: str) -> str:
    """Write/commit a file, returning the new HEAD SHA."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bug_body_with_baseline(
    *,
    sha: str,
    files: list[tuple[str, str]],
    cause_known: bool = True,
) -> str:
    """Render a bug body whose Diagnosis section carries baseline metadata.

    Uses the bullet-style diagnosis prose the shape gate's
    ``diagnosis_completeness`` heuristic recognizes (observed symptom /
    evidence / ruled out / confirmed cause / affected code path /
    fix-success criterion), with the ``**Baseline:**`` and
    ``**Inspected files:**`` lines this story adds nested directly under
    the ``## Diagnosis`` heading so groom can re-parse them.
    """
    inspected_lines = "\n".join(f"- `{p}` — `sha256:{h or 'unknown'}`" for p, h in files)
    cause_line = (
        "- Confirmed cause: root cause is X"
        if cause_known
        else "- Confirmed cause: not yet identified"
    )
    return (
        "## What happened\n\nThing breaks.\n\n"
        "## What was expected\n\nThing works.\n\n"
        "## Diagnosis\n\n"
        f"**Baseline:** `{sha}` captured at `2026-05-09T03:14:00Z`\n\n"
        "**Inspected files:**\n\n"
        f"{inspected_lines}\n\n"
        "- Observed symptom: thing breaks\n"
        "- Evidence: log line\n"
        "- Ruled out: alternative A\n"
        f"{cause_line}\n"
        "- Affected code path: src/a.py\n"
        "- Fix-success criterion: thing works again\n"
    )


# Helpers re-exported so the diagnose-flow test can import them too.
_ = (DiagnosisArtifact, Hypothesis, InspectedFile, render_artifact_markdown)


# ── Baseline metadata round-trip ─────────────────────────────────────────


def test_render_then_parse_round_trip():
    body = _bug_body_with_baseline(
        sha="abc1234",
        files=[("src/a.py", _sha256("hello\n")), ("src/b.py", _sha256("world\n"))],
    )
    meta = parse_baseline_metadata(body)
    assert meta is not None
    assert meta.baseline_sha == "abc1234"
    assert meta.baseline_captured_at == "2026-05-09T03:14:00Z"
    assert [f.path for f in meta.inspected_files] == ["src/a.py", "src/b.py"]
    assert meta.inspected_files[0].content_sha256 == _sha256("hello\n")


def test_parse_returns_none_when_no_baseline_line():
    body = "## Diagnosis\n\n### Observed symptom\n\nOld-style diagnosis with no baseline.\n"
    assert parse_baseline_metadata(body) is None


# ── Staleness evaluation against a real git repo ─────────────────────────


def test_fresh_when_baseline_equals_head(tmp_path: Path):
    _git_init(tmp_path)
    sha = _commit_file(tmp_path, "src/a.py", "hello\n", "init")
    body = _bug_body_with_baseline(sha=sha, files=[("src/a.py", _sha256("hello\n"))])
    report = evaluate_staleness(body, project_root=tmp_path)
    assert report.state is StalenessState.FRESH


def test_stale_when_inspected_file_changed_since_baseline(tmp_path: Path):
    _git_init(tmp_path)
    base_sha = _commit_file(tmp_path, "src/a.py", "hello\n", "v1")
    body = _bug_body_with_baseline(sha=base_sha, files=[("src/a.py", _sha256("hello\n"))])
    # Advance base + materially change the inspected file.
    _commit_file(tmp_path, "src/a.py", "hello world\n", "v2")
    report = evaluate_staleness(body, project_root=tmp_path)
    assert report.state is StalenessState.STALE
    assert "src/a.py" in report.changed_files


def test_unchanged_when_base_advanced_but_inspected_files_match(tmp_path: Path):
    _git_init(tmp_path)
    base_sha = _commit_file(tmp_path, "src/a.py", "hello\n", "v1")
    body = _bug_body_with_baseline(sha=base_sha, files=[("src/a.py", _sha256("hello\n"))])
    # Advance base via an unrelated file — inspected file content unchanged.
    _commit_file(tmp_path, "docs/README.md", "doc\n", "doc-only commit")
    report = evaluate_staleness(body, project_root=tmp_path)
    assert report.state is StalenessState.UNCHANGED
    assert report.changed_files == ()


def test_stale_when_inspected_file_deleted_since_baseline(tmp_path: Path):
    """Deletion is a material change: a path with a baseline digest that
    is positively absent at current must surface as STALE rather than be
    silently classified as untracked (which would let a confirmed-cause
    diagnosis proceed even though its evidence has been removed)."""
    _git_init(tmp_path)
    base_sha = _commit_file(tmp_path, "src/a.py", "hello\n", "v1")
    body = _bug_body_with_baseline(sha=base_sha, files=[("src/a.py", _sha256("hello\n"))])
    # Advance base by deleting the inspected file.
    subprocess.run(["git", "rm", "-q", "src/a.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove a.py"], cwd=tmp_path, check=True)
    report = evaluate_staleness(body, project_root=tmp_path)
    assert report.state is StalenessState.STALE
    assert "src/a.py" in report.changed_files
    assert "src/a.py" not in report.untracked_files


def test_groom_refuses_confirmed_cause_when_inspected_file_deleted(tmp_path: Path):
    """End-to-end: a confirmed-cause bug whose inspected file was removed
    by a later commit must be refused by groom — same refusal path as a
    content-change, since hash-equality treats deletion as material."""
    _git_init(tmp_path)
    base_sha = _commit_file(tmp_path, "src/a.py", "hello\n", "v1")
    body = _bug_body_with_baseline(
        sha=base_sha, files=[("src/a.py", _sha256("hello\n"))], cause_known=True
    )
    subprocess.run(["git", "rm", "-q", "src/a.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove a.py"], cwd=tmp_path, check=True)

    fetch = lambda _n, _r: {"title": "bug", "body": body, "labels": ["bug"]}  # noqa: E731
    result = run_groom(
        "1497",
        project_root=tmp_path,
        fetch_issue=fetch,
        edit_issue_body=lambda *_a: True,
    )
    assert result.action is GroomAction.REFUSED
    assert result.staleness is not None and result.staleness.is_stale
    assert "src/a.py" in result.staleness.changed_files
    ev = result.as_event()
    assert ev["staleness_verdict"] == "stale"
    assert "src/a.py" in ev["stale_files"]


def test_no_baseline_for_legacy_diagnosis(tmp_path: Path):
    body = "## Diagnosis\n\n### Observed symptom\n\nLegacy diagnosis without baseline.\n"
    report = evaluate_staleness(body, project_root=tmp_path)
    assert report.state is StalenessState.NO_BASELINE


# ── Groom integration: refusal, override, cause-unknown notice ───────────


def test_groom_refuses_confirmed_cause_with_stale_baseline(tmp_path: Path):
    _git_init(tmp_path)
    base_sha = _commit_file(tmp_path, "src/a.py", "hello\n", "v1")
    body = _bug_body_with_baseline(
        sha=base_sha, files=[("src/a.py", _sha256("hello\n"))], cause_known=True
    )
    _commit_file(tmp_path, "src/a.py", "hello world\n", "v2")  # invalidate baseline

    fetch = lambda _n, _r: {"title": "bug", "body": body, "labels": ["bug"]}  # noqa: E731
    edit = lambda *_a: True  # noqa: E731

    result = run_groom(
        "1497",
        project_root=tmp_path,
        fetch_issue=fetch,
        edit_issue_body=edit,
    )
    assert result.action is GroomAction.REFUSED
    assert result.bug_state is BugDiagnosisState.CONFIRMED_CAUSE
    assert result.staleness is not None and result.staleness.is_stale
    assert "stale" in (result.refusal_reason or "").lower()
    assert "forge diagnose 1497" in (result.refusal_reason or "")
    # Audit event surfaces the staleness verdict for downstream queries.
    ev = result.as_event()
    assert ev["staleness_verdict"] == "stale"
    assert ev["diagnosis_baseline_sha"] == base_sha
    assert "src/a.py" in ev["stale_files"]


def test_groom_confirm_override_lets_stale_confirmed_cause_proceed(tmp_path: Path):
    _git_init(tmp_path)
    base_sha = _commit_file(tmp_path, "src/a.py", "hello\n", "v1")
    body = _bug_body_with_baseline(
        sha=base_sha, files=[("src/a.py", _sha256("hello\n"))], cause_known=True
    )
    _commit_file(tmp_path, "src/a.py", "tampered\n", "v2")

    fetch = lambda _n, _r: {"title": "bug", "body": body, "labels": ["bug"]}  # noqa: E731

    result = run_groom(
        "1497",
        project_root=tmp_path,
        fetch_issue=fetch,
        edit_issue_body=lambda *_a: True,
        confirm_diagnosis_current=True,
    )
    # Override flips the refusal off — bug proceeds along the restructured path.
    assert result.action is GroomAction.RESTRUCTURED
    ev = result.as_event()
    assert ev["staleness_verdict"] == "stale"
    assert ev["confirm_diagnosis_current"] is True


def test_groom_cause_unknown_with_stale_baseline_stays_investigation_ready(
    tmp_path: Path,
):
    _git_init(tmp_path)
    base_sha = _commit_file(tmp_path, "src/a.py", "hello\n", "v1")
    body = _bug_body_with_baseline(
        sha=base_sha, files=[("src/a.py", _sha256("hello\n"))], cause_known=False
    )
    _commit_file(tmp_path, "src/a.py", "hello world\n", "v2")

    fetch = lambda _n, _r: {"title": "bug", "body": body, "labels": ["bug"]}  # noqa: E731

    result = run_groom(
        "1502",
        project_root=tmp_path,
        fetch_issue=fetch,
        edit_issue_body=lambda *_a: True,
    )
    # Cause-unknown invariant survives staleness — still investigation-ready.
    assert result.action is GroomAction.NORMALIZED_ONLY
    assert result.bug_state is BugDiagnosisState.CAUSE_UNKNOWN
    assert result.staleness is not None and result.staleness.is_stale
    assert result.staleness_notice is True
    # No-ready guarantee from ADR-0001.
    assert "add-label ready" not in (result.next_command or "")


# ── Audit substrate persistence ──────────────────────────────────────────


def test_record_readiness_event_persists_staleness_columns(tmp_path: Path):
    """Staleness verdict is queryable via dedicated columns, not just raw_json."""
    payload = {
        "kind": "groom",
        "issue_ref": "1497",
        "issue_type": "bug",
        "action": "refused",
        "applied": False,
        "bug_diagnosis_state": "confirmed-cause",
        "refusal_reason": "stale",
        "staleness_verdict": "stale",
        "diagnosis_baseline_sha": "abc1234",
        "stale_files": ["src/a.py"],
    }
    record_readiness_event(tmp_path, payload)
    conn = create_or_open(tmp_path)
    try:
        row = conn.execute(
            "SELECT staleness_verdict, diagnosis_baseline_sha FROM readiness_events"
        ).fetchone()
        assert row is not None
        assert row[0] == "stale"
        assert row[1] == "abc1234"
        events = list(iter_readiness_events(conn, kind="groom"))
        assert events[0]["stale_files"] == ["src/a.py"]
    finally:
        conn.close()


# ── Diagnose flow seam: baseline injection ───────────────────────────────


def test_diagnose_flow_injects_baseline_into_artifact(tmp_path: Path, monkeypatch):
    """The diagnose flow captures HEAD as the baseline SHA and hashes each
    inspected file against that SHA before persisting the artifact — so the
    rendered ``## Diagnosis`` body carries enough metadata for groom's
    staleness check."""
    from unittest.mock import patch

    import yaml as yaml_mod
    from coord_test_helpers import _make_config

    _git_init(tmp_path)
    head_sha = _commit_file(tmp_path, "src/scheduler.py", "def go(): pass\n", "init")

    agent_payload = {
        "observed_symptom": "x",
        "reproduction_or_evidence": "y",
        "hypotheses": [
            {
                "statement": "h",
                "status": "confirmed",
                "evidence": "e",
                "claim_verification": {
                    "verification_type": "source",
                    "detail": "Checked against the target repository source.",
                },
            },
        ],
        "confirmed_cause": "c",
        "confirmed_cause_verification": {
            "verification_type": "source",
            "detail": "Checked against the target repository source.",
        },
        "affected_code_path": "src/scheduler.py",
        "fix_success_criterion": "f",
        "notes": "",
        "inspected_files": ["src/scheduler.py"],
    }
    agent_yaml = "```yaml\n" + yaml_mod.safe_dump(agent_payload, sort_keys=False) + "```"

    config = _make_config(tmp_path)

    with (
        patch("theforge.coordinator.diagnose_flow._gh_fetch_issue") as mock_fetch,
        patch("theforge.coordinator.diagnose_flow.run_agent") as mock_agent,
    ):
        mock_fetch.return_value = {
            "number": 42,
            "title": "broken",
            "body": "story 3 never starts",
            "state": "OPEN",
        }
        from tests.test_diagnose_flow import _fake_agent_result  # type: ignore

        mock_agent.return_value = _fake_agent_result(agent_yaml)

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=42,
            config=config,
            project_root=tmp_path,
            output_destination="pr_to_body",
        )

    assert result.success
    artifact = result.state.artifact
    assert artifact is not None
    assert artifact.baseline_sha == head_sha
    assert artifact.baseline_captured_at  # non-empty timestamp
    assert len(artifact.inspected_files) == 1
    assert artifact.inspected_files[0].path == "src/scheduler.py"
    # Hash is computed against the baseline SHA via `git show`.
    expected_hash = hashlib.sha256(b"def go(): pass\n").hexdigest()
    assert artifact.inspected_files[0].content_sha256 == expected_hash

    # And the rendered markdown lands the metadata so groom can re-parse it.
    landed = (tmp_path / ".forge" / "diagnoses" / "issue-42.md").read_text()
    assert head_sha in landed
    assert "**Inspected files:**" in landed
    assert "src/scheduler.py" in landed


# Force pytest to register helper used above as importable.
_ = pytest  # noqa: F841
