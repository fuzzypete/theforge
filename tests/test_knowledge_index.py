from __future__ import annotations

from pathlib import Path

import yaml

from theforge.knowledge_index import KNOWLEDGE_INDEX_PATH, rebuild_knowledge_index


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
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return path


def _entry_ids(payload: dict[str, object]) -> list[str]:
    entries = payload["entries"]
    assert isinstance(entries, list)
    return [entry["run_id"] for entry in entries]


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


def test_entries_expose_lookup_fields_needed_by_next_story(tmp_path: Path) -> None:
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
    assert result.payload["schema_version"] == 1
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
        }
    ]
