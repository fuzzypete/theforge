from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from theforge.knowledge_admissibility import (
    REASON_CITED_SOURCE_DELETED,
    REASON_PROVENANCE_UNRESOLVED,
    REASON_RELEVANCE_INDETERMINATE,
    REASON_SOUNDNESS_INDETERMINATE,
    REASON_SOURCE_RUN_TAINTED,
    REASON_SOURCES_CHANGED,
    REASON_SOURCES_MOVED,
    STATUS_ADMISSIBLE,
    STATUS_ADMISSIBLE_WITH_REDUCED_RANK,
    STATUS_INADMISSIBLE,
)
from theforge.knowledge_index import KNOWLEDGE_INDEX_PATH, rebuild_knowledge_index


def _git(project_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_git_repo(project_root: Path) -> None:
    _git(project_root, "init", "-q", "-b", "main")
    _git(project_root, "config", "user.email", "test@example.com")
    _git(project_root, "config", "user.name", "Test")


def _commit_all(project_root: Path, message: str) -> str:
    _git(project_root, "add", ".")
    _git(project_root, "commit", "-q", "-m", message)
    return _git(project_root, "rev-parse", "HEAD")


def _write_run_record(
    project_root: Path,
    run_id: str,
    *,
    trust_status: str = "trusted",
    base_ref: str | None = None,
    head_ref: str | None = None,
    finding_ids: tuple[str, ...] = ("f-001",),
    file_paths: tuple[str, ...] = ("src/client.py",),
) -> Path:
    path = project_root / ".forge" / "audits" / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "trust_status": trust_status,
        "trust_checks": [],
        "finding_registry": [
            {
                "finding_id": finding_id,
                "cycle_first_seen": 1,
                "cycle_last_seen": 1,
                "file": file_paths[0] if file_paths else "",
                "severity": "P1",
                "description": f"finding {finding_id}",
                "disposition": "resolved",
            }
            for finding_id in finding_ids
        ],
        "reviews": [{"cycle": 1}],
        "phases": {"plan": {"plan_structured": {"steps": [{"id": "s-1", "description": "step"}]}}},
        "changed_files": {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "files": [{"path": path} for path in file_paths],
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_summary(
    project_root: Path,
    run_id: str,
    *,
    generated_at: str | None,
    slug: str,
    name: str,
    github_issue: int | None,
    work_type: str,
    complexity: str,
    complexity_score: int,
    contract_change: bool,
    domains: list[str],
    changed_files: list[str],
    learned_patterns: list[str],
    what_was_learned: list[dict] | None = None,
    authoritative_run_record: str | None = None,
) -> Path:
    path = project_root / ".forge" / "knowledge" / "summaries" / f"{run_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": generated_at,
        "story": {
            "slug": slug,
            "name": name,
            "github_issue": github_issue,
        },
        "story_shape": {
            "work_type": work_type,
            "complexity": complexity,
            "complexity_score": complexity_score,
            "contract_change": contract_change,
        },
        "domains": domains,
        "changed_files": changed_files,
        "learned_patterns": learned_patterns,
    }
    if authoritative_run_record is not None:
        payload["authoritative_run_record"] = authoritative_run_record
    if what_was_learned is not None:
        payload["what_was_learned"] = what_was_learned
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return path


def _entry_ids(payload: dict[str, object]) -> list[str]:
    entries = payload["entries"]
    assert isinstance(entries, list)
    return [entry["run_id"] for entry in entries]


def _entry_map(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    entries = payload["entries"]
    assert isinstance(entries, list)
    return {entry["run_id"]: entry for entry in entries if isinstance(entry, dict)}


def _summary_claim(*evidence: dict[str, object]) -> list[dict[str, object]]:
    return [{"claim": "learned something real", "evidence": list(evidence)}]


def test_rebuild_is_deterministic_and_order_is_stable(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        "run-review-prompt",
        generated_at="2026-08-14T11:00:00+00:00",
        slug="review-prompt",
        name="Review prompt tightening",
        github_issue=303,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["review", "prompting"],
        changed_files=["src/theforge/task/review_prompts.py"],
        learned_patterns=["prompt-contract"],
    )
    _write_summary(
        tmp_path,
        "run-api-retry",
        generated_at="2026-08-14T09:00:00+00:00",
        slug="api-retry",
        name="API retry hardening",
        github_issue=301,
        work_type="feature",
        complexity="medium",
        complexity_score=5,
        contract_change=False,
        domains=["backend", "api"],
        changed_files=["src/client.py", "tests/test_client.py"],
        learned_patterns=["retry", "timeout"],
    )
    _write_summary(
        tmp_path,
        "run-cli-config",
        generated_at=None,
        slug="cli-config",
        name="CLI config validation",
        github_issue=302,
        work_type="bugfix",
        complexity="small",
        complexity_score=3,
        contract_change=True,
        domains=["cli", "config"],
        changed_files=["src/theforge/cli/check_config.py"],
        learned_patterns=["config-guardrails"],
    )

    first = rebuild_knowledge_index(tmp_path)
    second = rebuild_knowledge_index(tmp_path)

    first_loaded = yaml.safe_load(first.path.read_text(encoding="utf-8"))
    second_loaded = yaml.safe_load(second.path.read_text(encoding="utf-8"))

    assert first.payload == second.payload
    assert first_loaded == second_loaded
    assert _entry_ids(first.payload) == [
        "run-cli-config",
        "run-api-retry",
        "run-review-prompt",
    ]


def test_deleting_the_index_and_rebuilding_produces_an_equivalent_payload(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        "run-api-retry",
        generated_at="2026-08-14T09:00:00+00:00",
        slug="api-retry",
        name="API retry hardening",
        github_issue=301,
        work_type="feature",
        complexity="medium",
        complexity_score=5,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
    )

    first = rebuild_knowledge_index(tmp_path)
    loaded_before = yaml.safe_load(first.path.read_text(encoding="utf-8"))
    first.path.unlink()

    second = rebuild_knowledge_index(tmp_path)
    loaded_after = yaml.safe_load(second.path.read_text(encoding="utf-8"))

    assert loaded_before == loaded_after == first.payload == second.payload


def test_malformed_and_invalid_summaries_are_skipped_with_diagnostics(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        "run-valid",
        generated_at="2026-08-14T09:00:00+00:00",
        slug="api-retry",
        name="API retry hardening",
        github_issue=301,
        work_type="feature",
        complexity="medium",
        complexity_score=5,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
    )

    summaries = tmp_path / ".forge" / "knowledge" / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / "bad-yaml.yaml").write_text("run_id: [unterminated\n", encoding="utf-8")
    (summaries / "bad-shape.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": "bad-shape",
                "generated_at": "2026-08-14T10:00:00+00:00",
                "story": [],
                "story_shape": {},
                "domains": ["backend"],
                "changed_files": ["src/client.py"],
                "learned_patterns": ["retry"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (summaries / "wrong-run-id.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": "different-run-id",
                "generated_at": "2026-08-14T10:00:00+00:00",
                "story": {"slug": "x", "name": "X", "github_issue": None},
                "story_shape": {},
                "domains": ["backend"],
                "changed_files": ["src/client.py"],
                "learned_patterns": ["retry"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = rebuild_knowledge_index(tmp_path)

    assert result.payload["source_count"] == 4
    assert result.payload["indexed_count"] == 1
    assert result.payload["skipped_count"] == 3
    assert _entry_ids(result.payload) == ["run-valid"]
    assert [diagnostic.path for diagnostic in result.diagnostics] == [
        ".forge/knowledge/summaries/bad-shape.yaml",
        ".forge/knowledge/summaries/bad-yaml.yaml",
        ".forge/knowledge/summaries/wrong-run-id.yaml",
    ]
    assert "story must be a mapping" in result.diagnostics[0].reason
    assert "invalid YAML" in result.diagnostics[1].reason
    assert "does not match summary filename" in result.diagnostics[2].reason


def test_unreadable_or_non_utf8_summaries_are_skipped_with_diagnostics(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        "run-valid",
        generated_at="2026-08-14T09:00:00+00:00",
        slug="api-retry",
        name="API retry hardening",
        github_issue=301,
        work_type="feature",
        complexity="medium",
        complexity_score=5,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
    )

    summaries = tmp_path / ".forge" / "knowledge" / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / "run-non-utf8.yaml").write_bytes(b"\xff\xfe\x00broken")

    result = rebuild_knowledge_index(tmp_path)

    assert result.payload["source_count"] == 2
    assert result.payload["indexed_count"] == 1
    assert result.payload["skipped_count"] == 1
    assert _entry_ids(result.payload) == ["run-valid"]
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].path == ".forge/knowledge/summaries/run-non-utf8.yaml"
    assert "unreadable summary:" in result.diagnostics[0].reason


def test_entries_expose_lookup_fields_and_fail_closed_verdicts(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        "run-api-retry",
        generated_at="2026-08-14T09:00:00+00:00",
        slug="api-retry",
        name="API retry hardening",
        github_issue=301,
        work_type="feature",
        complexity="medium",
        complexity_score=5,
        contract_change=False,
        domains=["backend", "api"],
        changed_files=["src/client.py", "tests/test_client.py"],
        learned_patterns=["retry", "timeout"],
    )

    result = rebuild_knowledge_index(tmp_path)

    assert result.path == tmp_path / KNOWLEDGE_INDEX_PATH
    assert result.diagnostics == ()
    assert result.payload["schema_version"] == 2
    assert result.payload["source_count"] == 1
    assert result.payload["indexed_count"] == 1
    assert result.payload["skipped_count"] == 0
    assert result.payload["entries"] == [
        {
            "run_id": "run-api-retry",
            "generated_at": "2026-08-14T09:00:00+00:00",
            "story": {
                "slug": "api-retry",
                "name": "API retry hardening",
                "github_issue": 301,
            },
            "story_shape": {
                "work_type": "feature",
                "complexity": "medium",
                "complexity_score": 5,
                "contract_change": False,
            },
            "domains": ["backend", "api"],
            "changed_files": ["src/client.py", "tests/test_client.py"],
            "learned_patterns": ["retry", "timeout"],
            "summary_path": ".forge/knowledge/summaries/run-api-retry.yaml",
            "admissibility_facts": {
                "source_run": {"tainted": False, "resolution": "indeterminate"},
                "provenance": {"resolution": "indeterminate"},
                "cited_sources": [],
            },
            "admissibility_verdict": {
                "status": STATUS_INADMISSIBLE,
                "rank": "excluded",
                "reasons": [REASON_SOUNDNESS_INDETERMINATE],
            },
        }
    ]


def test_unchanged_cited_source_is_admissible(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    src = tmp_path / "src" / "client.py"
    src.parent.mkdir(parents=True)
    src.write_text("value = 1\n", encoding="utf-8")
    head_ref = _commit_all(tmp_path, "seed")

    _write_run_record(tmp_path, "run-unchanged", base_ref=head_ref, head_ref=head_ref)
    _write_summary(
        tmp_path,
        "run-unchanged",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-unchanged.json",
        what_was_learned=_summary_claim(
            {"type": "file", "path": "src/client.py"},
            {"type": "review_finding", "finding_id": "f-001"},
        ),
    )

    result = rebuild_knowledge_index(tmp_path)
    entry = _entry_map(result.payload)["run-unchanged"]

    assert entry["admissibility_verdict"] == {"status": STATUS_ADMISSIBLE, "rank": "full"}
    assert entry["admissibility_facts"]["cited_sources"] == [
        {
            "cited_path": "src/client.py",
            "state": "unchanged",
            "current_path": "src/client.py",
            "commits_since_summary": 0,
        }
    ]


def test_changed_cited_source_is_down_ranked(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    src = tmp_path / "src" / "client.py"
    src.parent.mkdir(parents=True)
    src.write_text("value = 1\n", encoding="utf-8")
    baseline = _commit_all(tmp_path, "seed")
    _write_run_record(tmp_path, "run-changed", base_ref=baseline, head_ref=baseline)
    _write_summary(
        tmp_path,
        "run-changed",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-changed.json",
        what_was_learned=_summary_claim(
            {"type": "file", "path": "src/client.py"},
            {"type": "review_finding", "finding_id": "f-001"},
        ),
    )
    src.write_text("value = 2\n", encoding="utf-8")
    _commit_all(tmp_path, "follow-up")

    result = rebuild_knowledge_index(tmp_path)
    verdict = _entry_map(result.payload)["run-changed"]["admissibility_verdict"]

    assert verdict == {
        "status": STATUS_ADMISSIBLE_WITH_REDUCED_RANK,
        "rank": "reduced",
        "reasons": [REASON_SOURCES_CHANGED],
    }


def test_rename_with_continuity_is_down_ranked(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    old_path = tmp_path / "src" / "client.py"
    old_path.parent.mkdir(parents=True)
    old_path.write_text("value = 1\n", encoding="utf-8")
    baseline = _commit_all(tmp_path, "seed")
    _write_run_record(tmp_path, "run-moved", base_ref=baseline, head_ref=baseline)
    _write_summary(
        tmp_path,
        "run-moved",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-moved.json",
        what_was_learned=_summary_claim(
            {"type": "file", "path": "src/client.py"},
            {"type": "review_finding", "finding_id": "f-001"},
        ),
    )
    new_path = tmp_path / "src" / "phases" / "client.py"
    new_path.parent.mkdir(parents=True)
    _git(tmp_path, "mv", "src/client.py", "src/phases/client.py")
    _commit_all(tmp_path, "rename")

    result = rebuild_knowledge_index(tmp_path)
    entry = _entry_map(result.payload)["run-moved"]

    assert entry["admissibility_verdict"] == {
        "status": STATUS_ADMISSIBLE_WITH_REDUCED_RANK,
        "rank": "reduced",
        "reasons": [REASON_SOURCES_MOVED],
    }
    assert entry["admissibility_facts"]["cited_sources"] == [
        {
            "cited_path": "src/client.py",
            "state": "moved",
            "current_path": "src/phases/client.py",
            "commits_since_summary": 1,
        }
    ]


def test_deleted_cited_source_is_inadmissible(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    src = tmp_path / "src" / "client.py"
    src.parent.mkdir(parents=True)
    src.write_text("value = 1\n", encoding="utf-8")
    baseline = _commit_all(tmp_path, "seed")
    _write_run_record(tmp_path, "run-deleted", base_ref=baseline, head_ref=baseline)
    _write_summary(
        tmp_path,
        "run-deleted",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-deleted.json",
        what_was_learned=_summary_claim(
            {"type": "file", "path": "src/client.py"},
            {"type": "review_finding", "finding_id": "f-001"},
        ),
    )
    src.unlink()
    _commit_all(tmp_path, "delete")

    result = rebuild_knowledge_index(tmp_path)
    verdict = _entry_map(result.payload)["run-deleted"]["admissibility_verdict"]

    assert verdict == {
        "status": STATUS_INADMISSIBLE,
        "rank": "excluded",
        "reasons": [REASON_CITED_SOURCE_DELETED],
    }


def test_tainted_source_run_is_inadmissible(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    src = tmp_path / "src" / "client.py"
    src.parent.mkdir(parents=True)
    src.write_text("value = 1\n", encoding="utf-8")
    head_ref = _commit_all(tmp_path, "seed")

    _write_run_record(
        tmp_path,
        "run-tainted",
        trust_status="tainted",
        base_ref=head_ref,
        head_ref=head_ref,
    )
    _write_summary(
        tmp_path,
        "run-tainted",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-tainted.json",
        what_was_learned=_summary_claim(
            {"type": "file", "path": "src/client.py"},
            {"type": "review_finding", "finding_id": "f-001"},
        ),
    )

    result = rebuild_knowledge_index(tmp_path)
    verdict = _entry_map(result.payload)["run-tainted"]["admissibility_verdict"]

    assert verdict == {
        "status": STATUS_INADMISSIBLE,
        "rank": "excluded",
        "reasons": [REASON_SOURCE_RUN_TAINTED],
    }


def test_unresolved_provenance_is_inadmissible(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    src = tmp_path / "src" / "client.py"
    src.parent.mkdir(parents=True)
    src.write_text("value = 1\n", encoding="utf-8")
    head_ref = _commit_all(tmp_path, "seed")

    _write_run_record(tmp_path, "run-unresolved", base_ref=head_ref, head_ref=head_ref)
    _write_summary(
        tmp_path,
        "run-unresolved",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-unresolved.json",
        what_was_learned=_summary_claim({"type": "review_finding", "finding_id": "f-missing"}),
    )

    result = rebuild_knowledge_index(tmp_path)
    verdict = _entry_map(result.payload)["run-unresolved"]["admissibility_verdict"]

    assert verdict == {
        "status": STATUS_INADMISSIBLE,
        "rank": "excluded",
        "reasons": [REASON_PROVENANCE_UNRESOLVED],
    }


def test_non_git_history_absence_down_ranks_relevance(tmp_path: Path) -> None:
    src = tmp_path / "src" / "client.py"
    src.parent.mkdir(parents=True)
    src.write_text("value = 1\n", encoding="utf-8")

    _write_run_record(tmp_path, "run-no-history", file_paths=("src/client.py",))
    _write_summary(
        tmp_path,
        "run-no-history",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-no-history.json",
        what_was_learned=_summary_claim(
            {"type": "file", "path": "src/client.py"},
            {"type": "review_finding", "finding_id": "f-001"},
        ),
    )

    result = rebuild_knowledge_index(tmp_path)
    entry = _entry_map(result.payload)["run-no-history"]

    assert entry["admissibility_verdict"] == {
        "status": STATUS_ADMISSIBLE_WITH_REDUCED_RANK,
        "rank": "reduced",
        "reasons": [REASON_RELEVANCE_INDETERMINATE],
    }
    assert entry["admissibility_facts"]["cited_sources"] == [
        {
            "cited_path": "src/client.py",
            "state": "relevance_indeterminate",
            "current_path": "src/client.py",
        }
    ]


def test_missing_run_record_is_soundness_indeterminate(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        "run-missing-record",
        generated_at="2026-08-15T09:00:00+00:00",
        slug="retry-client",
        name="Retry client",
        github_issue=1,
        work_type="feature",
        complexity="small",
        complexity_score=2,
        contract_change=False,
        domains=["backend"],
        changed_files=["src/client.py"],
        learned_patterns=["retry"],
        authoritative_run_record=".forge/audits/runs/run-missing-record.json",
        what_was_learned=_summary_claim({"type": "review_finding", "finding_id": "f-001"}),
    )

    result = rebuild_knowledge_index(tmp_path)
    verdict = _entry_map(result.payload)["run-missing-record"]["admissibility_verdict"]

    assert verdict == {
        "status": STATUS_INADMISSIBLE,
        "rank": "excluded",
        "reasons": [REASON_SOUNDNESS_INDETERMINATE],
    }
