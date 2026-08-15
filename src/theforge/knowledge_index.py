"""Deterministic index over persisted run summaries.

This module builds Layer 3a as a disposable, rebuildable materialized view over
``.forge/knowledge/summaries/*.yaml`` and the deterministic facts needed to
judge those summaries before any consumer uses them. Schema v2 therefore reads:

- the persisted summary artifact itself;
- the summary's authoritative run record for trust/provenance facts; and
- repository history / tree state for cited-source movement facts.

It never calls a model and never depends on context assembly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from theforge.coordinator.trust_status import derive_trust_status, is_tainted
from theforge.knowledge_admissibility import (
    RESOLUTION_INDETERMINATE,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNRESOLVED,
    SOURCE_STATE_CHANGED,
    SOURCE_STATE_DELETED,
    SOURCE_STATE_MOVED,
    SOURCE_STATE_RELEVANCE_INDETERMINATE,
    SOURCE_STATE_SOUNDNESS_INDETERMINATE,
    SOURCE_STATE_UNCHANGED,
    KnowledgeSourceFact,
    KnowledgeSummaryFacts,
    evaluate_summary_admissibility,
    extract_summary_cited_paths,
)
from theforge.knowledge_summary import (
    EVIDENCE_REFERENCE_KEYS,
    SUMMARIES_DIR,
    extract_anchors,
)

KNOWLEDGE_INDEX_PATH = Path(".forge") / "knowledge" / "index.yaml"
KNOWLEDGE_INDEX_SCHEMA_VERSION = 2
_DEFAULT_RUN_RECORD_DIR = Path(".forge") / "audits" / "runs"
_GENERIC_REFERENCE_KEY = "reference"
_GIT_TIMEOUT_SECONDS = 5.0
_BASELINE_RESOLVED = "resolved"
_BASELINE_HISTORY_INDETERMINATE = "history_indeterminate"
_BASELINE_SOURCE_IDENTITY_UNRESOLVED = "source_identity_unresolved"


@dataclass(frozen=True)
class KnowledgeIndexDiagnostic:
    path: str
    reason: str


@dataclass(frozen=True)
class KnowledgeIndexBuildResult:
    path: Path
    payload: dict[str, object]
    diagnostics: tuple[KnowledgeIndexDiagnostic, ...]


@dataclass(frozen=True)
class _SourceBaseline:
    status: str
    baseline_ref: str | None = None
    path_at_baseline: str | None = None


@dataclass(frozen=True)
class _GitDiffSnapshot:
    deleted_paths: frozenset[str]
    rename_targets: dict[str, str]


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
        except (OSError, UnicodeDecodeError) as exc:
            diagnostics.append(
                KnowledgeIndexDiagnostic(path=rel_path, reason=f"unreadable summary: {exc}")
            )
            continue
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

        entries.append(
            _build_entry_with_facts(
                project_root,
                summary,
                validated,
                summary_path=rel_path,
            )
        )

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


def _build_entry_with_facts(
    project_root: Path,
    raw_summary: dict[str, Any],
    validated_summary: dict[str, Any],
    *,
    summary_path: str,
) -> dict[str, object]:
    entry = _build_entry(validated_summary, summary_path=summary_path)
    facts = _build_admissibility_facts(raw_summary, project_root=project_root)
    verdict = evaluate_summary_admissibility(raw_summary, facts)
    entry["admissibility_facts"] = facts.to_dict()
    entry["admissibility_verdict"] = verdict.to_dict()
    return entry


def _build_admissibility_facts(
    summary: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> KnowledgeSummaryFacts:
    run_record = None
    source_run_resolution = RESOLUTION_INDETERMINATE
    source_run_tainted = False
    provenance_resolution = RESOLUTION_INDETERMINATE
    cited_sources: tuple[KnowledgeSourceFact, ...] = ()

    if project_root is not None:
        run_record = _load_run_record(project_root, summary)
        if run_record is not None:
            source_run_resolution = RESOLUTION_RESOLVED
            source_run_tainted = _run_record_is_tainted(run_record)
            provenance_resolution = _provenance_resolution(summary, run_record)
            cited_sources = _cited_source_facts(project_root, summary, run_record)
        else:
            provenance_resolution = RESOLUTION_INDETERMINATE

    return KnowledgeSummaryFacts(
        source_run_tainted=source_run_tainted,
        source_run_resolution=source_run_resolution,
        provenance_resolution=provenance_resolution,
        cited_sources=cited_sources,
    )


def _summary_run_record_path(project_root: Path, summary: dict[str, Any]) -> Path:
    raw = summary.get("authoritative_run_record")
    run_id = str(summary.get("run_id") or "").strip()
    if isinstance(raw, str) and raw.strip():
        return project_root / raw
    return project_root / _DEFAULT_RUN_RECORD_DIR / f"{run_id}.json"


def _load_run_record(project_root: Path, summary: dict[str, Any]) -> dict[str, Any] | None:
    path = _summary_run_record_path(project_root, summary)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _run_record_is_tainted(run_record: dict[str, Any]) -> bool:
    trust_status = run_record.get("trust_status")
    if isinstance(trust_status, str):
        return is_tainted(trust_status)
    return is_tainted(derive_trust_status(run_record.get("trust_checks")))


def _provenance_resolution(summary: dict[str, Any], run_record: dict[str, Any]) -> str:
    anchors = extract_anchors(run_record)
    learned = summary.get("what_was_learned")
    if not isinstance(learned, list):
        return RESOLUTION_INDETERMINATE

    unresolved = False
    for claim in learned:
        if not isinstance(claim, dict):
            return RESOLUTION_INDETERMINATE
        evidence = claim.get("evidence")
        if not isinstance(evidence, list):
            return RESOLUTION_INDETERMINATE
        for item in evidence:
            if not isinstance(item, dict):
                return RESOLUTION_INDETERMINATE
            evidence_type = str(item.get("type") or "").strip().lower()
            key = EVIDENCE_REFERENCE_KEYS.get(evidence_type)
            if key is None:
                return RESOLUTION_INDETERMINATE
            reference = str(item.get(key) or item.get(_GENERIC_REFERENCE_KEY) or "").strip()
            if not reference:
                return RESOLUTION_INDETERMINATE
            if not anchors.resolves(evidence_type, reference):
                unresolved = True
    return RESOLUTION_UNRESOLVED if unresolved else RESOLUTION_RESOLVED


def _cited_source_facts(
    project_root: Path,
    summary: dict[str, Any],
    run_record: dict[str, Any],
) -> tuple[KnowledgeSourceFact, ...]:
    citations = extract_summary_cited_paths(summary)
    if not citations:
        return ()

    changed_paths = _run_record_changed_paths(run_record)
    if not _git_history_available(project_root):
        return tuple(
            KnowledgeSourceFact(
                cited_path=path,
                state=SOURCE_STATE_RELEVANCE_INDETERMINATE,
                current_path=path if (project_root / path).exists() else None,
            )
            for path in citations
        )

    changed = run_record.get("changed_files")
    changed_block = changed if isinstance(changed, dict) else {}
    base_ref = _git_ref_text(changed_block.get("base_ref"))
    head_ref = _git_ref_text(changed_block.get("head_ref"))
    diff_cache: dict[str, _GitDiffSnapshot | None] = {}

    facts: list[KnowledgeSourceFact] = []
    for cited_path in citations:
        baseline = _resolve_source_baseline(
            project_root,
            cited_path,
            head_ref=head_ref,
            base_ref=base_ref,
        )
        if baseline.status == _BASELINE_HISTORY_INDETERMINATE and cited_path in changed_paths:
            facts.append(
                KnowledgeSourceFact(
                    cited_path=cited_path,
                    state=SOURCE_STATE_RELEVANCE_INDETERMINATE,
                    current_path=_tracked_head_path(project_root, cited_path),
                )
            )
            continue
        if baseline.status != _BASELINE_RESOLVED:
            facts.append(
                KnowledgeSourceFact(
                    cited_path=cited_path,
                    state=SOURCE_STATE_SOUNDNESS_INDETERMINATE,
                )
            )
            continue
        assert baseline.baseline_ref is not None
        assert baseline.path_at_baseline is not None
        baseline_ref = baseline.baseline_ref
        path_at_baseline = baseline.path_at_baseline
        if not _git_is_ancestor(project_root, baseline_ref, "HEAD"):
            facts.append(
                KnowledgeSourceFact(
                    cited_path=cited_path,
                    state=SOURCE_STATE_RELEVANCE_INDETERMINATE,
                    current_path=_tracked_head_path(project_root, cited_path),
                )
            )
            continue

        facts.append(
            _materialize_source_fact(
                project_root,
                cited_path,
                baseline_ref=baseline_ref,
                path_at_baseline=path_at_baseline,
                diff_cache=diff_cache,
            )
        )

    facts.sort(key=lambda item: (item.cited_path, item.current_path or "", item.state))
    return tuple(facts)


def _git_ref_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _resolve_source_baseline(
    project_root: Path,
    cited_path: str,
    *,
    head_ref: str | None,
    base_ref: str | None,
) -> _SourceBaseline:
    saw_candidate_ref = False
    saw_unavailable_ref = False
    for ref in (head_ref, base_ref):
        if ref is None:
            continue
        saw_candidate_ref = True
        if not _git_ref_exists(project_root, ref):
            saw_unavailable_ref = True
            continue
        exists = _git_path_exists_at_ref(project_root, ref, cited_path)
        if exists:
            return _SourceBaseline(
                status=_BASELINE_RESOLVED,
                baseline_ref=ref,
                path_at_baseline=cited_path,
            )
    if saw_unavailable_ref or not saw_candidate_ref:
        return _SourceBaseline(status=_BASELINE_HISTORY_INDETERMINATE)
    return _SourceBaseline(status=_BASELINE_SOURCE_IDENTITY_UNRESOLVED)


def _materialize_source_fact(
    project_root: Path,
    cited_path: str,
    *,
    baseline_ref: str,
    path_at_baseline: str,
    diff_cache: dict[str, _GitDiffSnapshot | None],
) -> KnowledgeSourceFact:
    snapshot = _git_diff_snapshot(project_root, baseline_ref, diff_cache=diff_cache)
    rename_target = snapshot.rename_targets.get(path_at_baseline) if snapshot is not None else None
    if rename_target is not None:
        return KnowledgeSourceFact(
            cited_path=cited_path,
            state=SOURCE_STATE_MOVED,
            current_path=rename_target,
            commits_since_summary=_git_commit_count(
                project_root,
                baseline_ref,
                [path_at_baseline, rename_target],
            ),
        )

    current_path = _tracked_head_path(project_root, cited_path)
    if current_path is not None:
        commits_since_summary = _git_commit_count(project_root, baseline_ref, [cited_path])
        if commits_since_summary is None:
            return KnowledgeSourceFact(
                cited_path=cited_path,
                state=SOURCE_STATE_RELEVANCE_INDETERMINATE,
                current_path=cited_path,
            )
        state = SOURCE_STATE_UNCHANGED if commits_since_summary == 0 else SOURCE_STATE_CHANGED
        return KnowledgeSourceFact(
            cited_path=cited_path,
            state=state,
            current_path=cited_path,
            commits_since_summary=commits_since_summary,
        )

    if snapshot is None:
        return KnowledgeSourceFact(
            cited_path=cited_path,
            state=SOURCE_STATE_RELEVANCE_INDETERMINATE,
        )
    if path_at_baseline in snapshot.deleted_paths:
        return KnowledgeSourceFact(
            cited_path=cited_path,
            state=SOURCE_STATE_DELETED,
            commits_since_summary=_git_commit_count(
                project_root,
                baseline_ref,
                [path_at_baseline],
            ),
        )
    return KnowledgeSourceFact(
        cited_path=cited_path,
        state=SOURCE_STATE_RELEVANCE_INDETERMINATE,
    )


def _run_record_changed_paths(run_record: dict[str, Any]) -> frozenset[str]:
    changed = run_record.get("changed_files")
    changed_block = changed if isinstance(changed, dict) else {}
    raw_files = changed_block.get("files")
    if not isinstance(raw_files, list):
        return frozenset()

    paths = {
        path
        for entry in raw_files
        if isinstance(entry, dict)
        for path in (str(entry.get("path") or "").strip(),)
        if path
    }
    return frozenset(paths)


def _git_history_available(project_root: Path) -> bool:
    result = _run_git(project_root, ["rev-parse", "--verify", "HEAD^{commit}"])
    return result.returncode == 0


def _git_ref_exists(project_root: Path, ref: str) -> bool:
    result = _run_git(project_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    return result.returncode == 0


def _git_is_ancestor(project_root: Path, older: str, newer: str) -> bool:
    result = _run_git(project_root, ["merge-base", "--is-ancestor", older, newer])
    return result.returncode == 0


def _git_path_exists_at_ref(project_root: Path, ref: str, path: str) -> bool:
    result = _run_git(project_root, ["cat-file", "-e", f"{ref}:{path}"])
    return result.returncode == 0


def _tracked_head_path(project_root: Path, path: str) -> str | None:
    return path if _git_path_exists_at_ref(project_root, "HEAD", path) else None


def _git_diff_snapshot(
    project_root: Path,
    baseline_ref: str,
    *,
    diff_cache: dict[str, _GitDiffSnapshot | None],
) -> _GitDiffSnapshot | None:
    cached = diff_cache.get(baseline_ref)
    if baseline_ref in diff_cache:
        return cached

    result = _run_git(
        project_root,
        [
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-status",
            "-M",
            "--find-renames",
            baseline_ref,
            "HEAD",
        ],
    )
    if result.returncode != 0:
        diff_cache[baseline_ref] = None
        return None

    deleted_paths: set[str] = set()
    rename_targets: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            rename_targets[parts[1]] = parts[2]
            continue
        if len(parts) >= 2 and parts[0] == "D":
            deleted_paths.add(parts[1])

    snapshot = _GitDiffSnapshot(
        deleted_paths=frozenset(deleted_paths),
        rename_targets=rename_targets,
    )
    diff_cache[baseline_ref] = snapshot
    return snapshot


def _git_commit_count(project_root: Path, baseline_ref: str, paths: list[str]) -> int | None:
    if not paths:
        return 0
    result = _run_git(project_root, ["rev-list", "--count", f"{baseline_ref}..HEAD", "--", *paths])
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return int(text) if text.isdigit() else None


def _run_git(project_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        return subprocess.run(  # noqa: S603
            command,
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout="",
            stderr=str(exc),
        )
