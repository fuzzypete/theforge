"""Deterministic index over persisted run summaries.

This module builds Layer 3a from Layer 2 artifacts only: a disposable,
rebuildable materialized view over ``.forge/knowledge/summaries/*.yaml``.
It never reads audits and never calls a model. If a consumer needs an
authoritative fact, it must follow the summary's backlink to the run record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from theforge.knowledge_summary import SUMMARIES_DIR

KNOWLEDGE_INDEX_PATH = Path(".forge") / "knowledge" / "index.yaml"
KNOWLEDGE_INDEX_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class KnowledgeIndexDiagnostic:
    path: str
    reason: str


@dataclass(frozen=True)
class KnowledgeIndexBuildResult:
    path: Path
    payload: dict[str, object]
    diagnostics: tuple[KnowledgeIndexDiagnostic, ...]


def _summary_sort_key(entry: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(entry.get("generated_at") or ""),
        str(entry.get("run_id") or ""),
        str(entry.get("summary_path") or ""),
    )


def _expect_mapping(data: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a mapping")
    return data


def _expect_list(data: Any, *, label: str) -> list[Any]:
    if not isinstance(data, list):
        raise ValueError(f"{label} must be a list")
    return data


def _validate_summary(summary: Any, *, expected_run_id: str) -> dict[str, Any]:
    data = _expect_mapping(summary, label="summary root")

    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if run_id != expected_run_id:
        raise ValueError(f"run_id {run_id!r} does not match summary filename {expected_run_id!r}")

    if "generated_at" not in data:
        raise ValueError("generated_at is required")
    generated_at = data.get("generated_at")
    if generated_at is not None and not isinstance(generated_at, str):
        raise ValueError("generated_at must be a string or null")

    story = _expect_mapping(data.get("story"), label="story")
    story_shape = _expect_mapping(data.get("story_shape"), label="story_shape")
    domains = _expect_list(data.get("domains"), label="domains")
    changed_files = _expect_list(data.get("changed_files"), label="changed_files")
    learned_patterns = _expect_list(data.get("learned_patterns"), label="learned_patterns")

    if any(not isinstance(value, str) for value in domains):
        raise ValueError("domains entries must be strings")
    if any(not isinstance(value, str) for value in changed_files):
        raise ValueError("changed_files entries must be strings")
    if any(not isinstance(value, str) for value in learned_patterns):
        raise ValueError("learned_patterns entries must be strings")

    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "story": {
            "slug": story.get("slug"),
            "name": story.get("name"),
            "github_issue": story.get("github_issue"),
        },
        "story_shape": story_shape,
        "domains": domains,
        "changed_files": changed_files,
        "learned_patterns": learned_patterns,
    }


def _build_entry(summary: dict[str, Any], *, summary_path: str) -> dict[str, object]:
    return {
        "run_id": summary["run_id"],
        "generated_at": summary["generated_at"],
        "story": summary["story"],
        "story_shape": summary["story_shape"],
        "domains": list(summary["domains"]),
        "changed_files": list(summary["changed_files"]),
        "learned_patterns": list(summary["learned_patterns"]),
        "summary_path": summary_path,
    }


def rebuild_knowledge_index(project_root: Path) -> KnowledgeIndexBuildResult:
    """Rebuild ``.forge/knowledge/index.yaml`` from persisted summary artifacts."""
    project_root = Path(project_root)
    summaries_root = project_root / SUMMARIES_DIR
    index_path = project_root / KNOWLEDGE_INDEX_PATH

    entries: list[dict[str, object]] = []
    diagnostics: list[KnowledgeIndexDiagnostic] = []
    summary_paths = sorted(summaries_root.glob("*.yaml")) if summaries_root.exists() else []

    for path in summary_paths:
        rel_path = str(path.relative_to(project_root))
        try:
            summary = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            diagnostics.append(
                KnowledgeIndexDiagnostic(path=rel_path, reason=f"invalid YAML: {exc}")
            )
            continue

        try:
            validated = _validate_summary(summary, expected_run_id=path.stem)
        except ValueError as exc:
            diagnostics.append(KnowledgeIndexDiagnostic(path=rel_path, reason=str(exc)))
            continue

        entries.append(_build_entry(validated, summary_path=rel_path))

    entries.sort(key=_summary_sort_key)
    payload: dict[str, object] = {
        "schema_version": KNOWLEDGE_INDEX_SCHEMA_VERSION,
        "source_count": len(summary_paths),
        "indexed_count": len(entries),
        "skipped_count": len(diagnostics),
        "entries": entries,
    }

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return KnowledgeIndexBuildResult(
        path=index_path,
        payload=payload,
        diagnostics=tuple(diagnostics),
    )
