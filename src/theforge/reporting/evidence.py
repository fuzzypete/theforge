"""Collect an observed run's evidence from the project it was observed in.

Pure Python, filesystem-read-only: the collector reads the observing project's
``.forge/`` tree for one run id and returns what it found plus an explicit
reason for everything it did not. It never falls back to the reading machine's
own configuration — the run's forge version, runtime identity, and resolved
configuration come from the recorded run artifacts or are reported missing.

The run id may name either domain:

* a *story* run — ``.forge/audits/runs/<run_id>.json`` exists;
* a *sprint* run — ``.forge/logs/<sprint>/run-<run_id>-summary.yaml`` exists,
  whose ``stories[*].story_run_id`` rows name the per-story records.

These are different id spaces and the collector resolves them separately; a
sprint id reaches every story's record through the run-keyed summary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from theforge.coordinator.agent_identity import invocation_identity_rows

# Per-artifact content cap. Larger artifacts are carried truncated (and say so)
# rather than dropped — a truncated log still answers most questions.
MAX_ARTIFACT_CHARS = 200_000

KIND_RUN_LOG = "run_log"
KIND_RUN_RECORD = "run_record"
KIND_STORY_AUDIT = "story_audit"
KIND_SPRINT_STATE = "sprint_state"
KIND_REVIEWER_OUTPUTS = "reviewer_outputs"
KIND_STORY_BODY = "story_body"
KIND_INTAKE_CANDIDATES = "intake_candidates"
KIND_RUNTIME_IDENTITY = "runtime_identity"
KIND_RESOLVED_CONFIG = "resolved_configuration"

# Display order for the manifest. Also the closed set of evidence kinds: a kind
# is either available (one or more artifacts) or missing (with a reason), and
# may be both when only part of it was recoverable.
EVIDENCE_KINDS: tuple[tuple[str, str], ...] = (
    (KIND_RUN_LOG, "run log"),
    (KIND_RUN_RECORD, "run record"),
    (KIND_STORY_AUDIT, "per-story audit"),
    (KIND_SPRINT_STATE, "sprint state"),
    (KIND_REVIEWER_OUTPUTS, "reviewer outputs"),
    (KIND_STORY_BODY, "story body"),
    (KIND_INTAKE_CANDIDATES, "intake candidate artifacts"),
    (KIND_RUNTIME_IDENTITY, "runtime identity"),
    (KIND_RESOLVED_CONFIG, "resolved configuration"),
)

_KIND_LABELS: dict[str, str] = dict(EVIDENCE_KINDS)
_KIND_ORDER: dict[str, int] = {kind: i for i, (kind, _) in enumerate(EVIDENCE_KINDS)}


class EvidenceError(RuntimeError):
    """The requested run could not be located in the observing project."""


@dataclass(frozen=True)
class EvidenceArtifact:
    """One captured artifact. ``content`` is always real captured text."""

    kind: str
    name: str
    content: str
    path: str | None = None
    truncated_from: int | None = None

    @property
    def label(self) -> str:
        return _KIND_LABELS.get(self.kind, self.kind)

    @property
    def truncated(self) -> bool:
        return self.truncated_from is not None


@dataclass(frozen=True)
class MissingEvidence:
    """One part of the record that could not be captured, and why."""

    kind: str
    reason: str
    name: str | None = None

    @property
    def label(self) -> str:
        return _KIND_LABELS.get(self.kind, self.kind)

    @property
    def display(self) -> str:
        if self.name:
            return f"{self.label} ({self.name}): {self.reason}"
        return f"{self.label}: {self.reason}"


@dataclass(frozen=True)
class RunEvidence:
    """Everything the report carries about one observed run."""

    run_id: str
    run_kind: str  # "story" | "sprint"
    forge_version: str | None
    observed_project: str | None
    sprint_name: str | None
    sprint_id: str | None
    story_slugs: tuple[str, ...]
    story_run_ids: tuple[str, ...]
    config_summary: str
    artifacts: tuple[EvidenceArtifact, ...]
    missing: tuple[MissingEvidence, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def available_labels(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for artifact in sorted(self.artifacts, key=lambda a: _KIND_ORDER.get(a.kind, 99)):
            seen.setdefault(artifact.label, None)
        return tuple(seen)

    def with_missing(self, extra: list[MissingEvidence]) -> RunEvidence:
        """Return a copy carrying additional missing entries (e.g. payload drops)."""
        if not extra:
            return self
        return replace(self, missing=self.missing + tuple(extra))


# ── collection ────────────────────────────────────────────────────────────────


def collect_run_evidence(
    project_root: Path,
    run_id: str,
    *,
    max_artifact_chars: int = MAX_ARTIFACT_CHARS,
) -> RunEvidence:
    """Capture ``run_id``'s evidence from ``project_root``.

    Raises :class:`EvidenceError` when the observing project has no record of
    the run at all — a report about a run this project never saw would carry no
    evidence and should not be filed.
    """
    forge_dir = project_root / ".forge"
    artifacts: list[EvidenceArtifact] = []
    missing: list[MissingEvidence] = []

    resolution = _resolve_run(forge_dir, run_id)
    records = resolution.records

    # Runtime identity and resolved configuration come only from the recorded
    # run — a reader's own forge.yaml or model profiles must never stand in.
    forge_version = _first_forge_version(records)

    _collect_sprint_state(forge_dir, resolution, artifacts, missing, max_artifact_chars)

    if not resolution.story_run_ids:
        for kind in (
            KIND_RUN_LOG,
            KIND_RUN_RECORD,
            KIND_STORY_AUDIT,
            KIND_REVIEWER_OUTPUTS,
            KIND_STORY_BODY,
            KIND_INTAKE_CANDIDATES,
            KIND_RUNTIME_IDENTITY,
            KIND_RESOLVED_CONFIG,
        ):
            missing.append(
                MissingEvidence(
                    kind=kind,
                    reason=(
                        f"no story run ids recorded for {run_id}; "
                        "the run-keyed sprint summary that names them was not found"
                    ),
                )
            )
    for slug, story_run_id in zip(resolution.story_slugs, resolution.story_run_ids, strict=True):
        _collect_story(
            forge_dir,
            sprint_name=resolution.sprint_name,
            slug=slug,
            story_run_id=story_run_id,
            record=records.get(story_run_id),
            artifacts=artifacts,
            missing=missing,
            max_artifact_chars=max_artifact_chars,
        )

    # The sprint's own run log covers every story in it, so a story-domain
    # report carries it too rather than reporting "no run log" for a run whose
    # log exists one level up.
    _collect_sprint_run_log(forge_dir, resolution, artifacts, missing, max_artifact_chars)

    return RunEvidence(
        run_id=run_id,
        run_kind=resolution.run_kind,
        forge_version=forge_version,
        observed_project=read_observed_project(project_root),
        sprint_name=resolution.sprint_name,
        sprint_id=resolution.sprint_id,
        story_slugs=resolution.story_slugs,
        story_run_ids=resolution.story_run_ids,
        config_summary=_config_summary(records, artifacts, missing),
        artifacts=tuple(artifacts),
        missing=tuple(sorted(missing, key=lambda m: (_KIND_ORDER.get(m.kind, 99), m.name or ""))),
    )


@dataclass(frozen=True)
class _Resolution:
    run_kind: str
    sprint_name: str | None
    sprint_id: str | None
    story_slugs: tuple[str, ...]
    story_run_ids: tuple[str, ...]
    records: dict[str, dict]
    summary_path: Path | None
    sprint_audit_path: Path | None


def _resolve_run(forge_dir: Path, run_id: str) -> _Resolution:
    record_path = forge_dir / "audits" / "runs" / f"{run_id}.json"
    if record_path.is_file():
        record = _load_json(record_path) or {}
        sprint_name = _str_or_none(record.get("sprint_name"))
        slug = _str_or_none((record.get("task") or {}).get("slug")) or run_id
        return _Resolution(
            run_kind="story",
            sprint_name=sprint_name,
            sprint_id=_str_or_none(record.get("sprint_id")),
            story_slugs=(slug,),
            story_run_ids=(run_id,),
            records={run_id: record},
            summary_path=_summary_for_story(forge_dir, sprint_name, run_id),
            sprint_audit_path=None,
        )

    summary_path = _find_sprint_summary(forge_dir, run_id)
    sprint_audit_path = forge_dir / "audits" / f"run-{run_id}-sprint-audit.yaml"
    if summary_path is None and not sprint_audit_path.is_file():
        raise EvidenceError(
            f"no run '{run_id}' recorded in this project: neither "
            f".forge/audits/runs/{run_id}.json nor a sprint summary "
            f".forge/logs/*/run-{run_id}-summary.yaml exists"
        )

    slugs: list[str] = []
    story_run_ids: list[str] = []
    sprint_name: str | None = None
    sprint_id: str | None = None

    summary = _load_yaml(summary_path) if summary_path is not None else None
    if summary_path is not None:
        sprint_name = summary_path.parent.name
    if isinstance(summary, dict):
        block = summary.get("sprint")
        if isinstance(block, dict):
            sprint_id = _str_or_none(block.get("sprint_id"))
            sprint_name = _str_or_none(block.get("name")) or sprint_name
        for row in summary.get("stories") or []:
            if not isinstance(row, dict):
                continue
            story_run_id = _str_or_none(row.get("story_run_id"))
            if not story_run_id:
                continue
            story_run_ids.append(story_run_id)
            slugs.append(_str_or_none(row.get("slug")) or story_run_id)

    if sprint_name is None and sprint_audit_path.is_file():
        audit = _load_yaml(sprint_audit_path)
        if isinstance(audit, dict) and isinstance(audit.get("sprint"), dict):
            sprint_name = _str_or_none(audit["sprint"].get("name"))
            sprint_id = sprint_id or _str_or_none(audit["sprint"].get("sprint_id"))

    records: dict[str, dict] = {}
    for story_run_id in story_run_ids:
        path = forge_dir / "audits" / "runs" / f"{story_run_id}.json"
        if path.is_file():
            records[story_run_id] = _load_json(path) or {}

    return _Resolution(
        run_kind="sprint",
        sprint_name=sprint_name,
        sprint_id=sprint_id,
        story_slugs=tuple(slugs),
        story_run_ids=tuple(story_run_ids),
        records=records,
        summary_path=summary_path,
        sprint_audit_path=sprint_audit_path if sprint_audit_path.is_file() else None,
    )


def _collect_story(
    forge_dir: Path,
    *,
    sprint_name: str | None,
    slug: str,
    story_run_id: str,
    record: dict | None,
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
    max_artifact_chars: int,
) -> None:
    story_dir = forge_dir / "logs" / sprint_name / slug if sprint_name else None

    # run log — searched across the log tree; parallel sprints and standalone
    # runs place it in different directories, and a story's per-phase logs
    # (dev iterations, raw preflight) live beside its audit.
    logs = sorted((forge_dir / "logs").glob(f"**/run-{story_run_id}.log"))
    if story_dir is not None:
        logs += sorted(story_dir.glob("*.log"))
    if logs:
        for path in logs:
            artifacts.append(
                _artifact(KIND_RUN_LOG, forge_dir, path, max_artifact_chars, tail=True)
            )
    else:
        missing.append(
            MissingEvidence(
                kind=KIND_RUN_LOG,
                name=slug,
                reason=f"no .forge/logs/**/run-{story_run_id}.log in this project",
            )
        )

    record_path = forge_dir / "audits" / "runs" / f"{story_run_id}.json"
    if record is not None and record_path.is_file():
        artifacts.append(_artifact(KIND_RUN_RECORD, forge_dir, record_path, max_artifact_chars))
    else:
        missing.append(
            MissingEvidence(
                kind=KIND_RUN_RECORD,
                name=slug,
                reason=f"no .forge/audits/runs/{story_run_id}.json in this project",
            )
        )

    audit_path = story_dir / "audit.yaml" if story_dir else None
    if audit_path is not None and audit_path.is_file():
        artifacts.append(_artifact(KIND_STORY_AUDIT, forge_dir, audit_path, max_artifact_chars))
    else:
        missing.append(
            MissingEvidence(
                kind=KIND_STORY_AUDIT,
                name=slug,
                reason=(
                    f"no audit.yaml under .forge/logs/{sprint_name}/{slug}/"
                    if story_dir
                    else "no sprint log directory recorded for this run"
                ),
            )
        )

    reviewer_paths: list[Path] = []
    if story_dir is not None:
        reviewer_paths = sorted(story_dir.glob("review-cycle-*/*.yaml"))
    if reviewer_paths:
        for path in reviewer_paths:
            artifacts.append(_artifact(KIND_REVIEWER_OUTPUTS, forge_dir, path, max_artifact_chars))
    else:
        missing.append(
            MissingEvidence(
                kind=KIND_REVIEWER_OUTPUTS,
                name=slug,
                reason=(
                    f"no review-cycle-*/*.yaml under .forge/logs/{sprint_name}/{slug}/"
                    if story_dir
                    else "no sprint log directory recorded for this run"
                ),
            )
        )

    _collect_story_body(record, slug, artifacts, missing)
    _collect_intake_candidates(forge_dir, record, slug, artifacts, missing, max_artifact_chars)
    _collect_runtime_identity(record, slug, artifacts, missing)
    _collect_resolved_configuration(record, slug, artifacts, missing)


def _collect_story_body(
    record: dict | None,
    slug: str,
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
) -> None:
    story_text = _str_or_none(_task_block(record).get("story_text"))
    if story_text:
        artifacts.append(
            EvidenceArtifact(
                kind=KIND_STORY_BODY,
                name=f"{slug}-story.md",
                content=story_text,
                path=None,
            )
        )
        return
    missing.append(
        MissingEvidence(
            kind=KIND_STORY_BODY,
            name=slug,
            reason=(
                "run record carries no task.story_text"
                if record is not None
                else "no run record to read the story body from"
            ),
        )
    )


def _collect_intake_candidates(
    forge_dir: Path,
    record: dict | None,
    slug: str,
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
    max_artifact_chars: int,
) -> None:
    issue = _task_block(record).get("github_issue")
    if not isinstance(issue, int):
        missing.append(
            MissingEvidence(
                kind=KIND_INTAKE_CANDIDATES,
                name=slug,
                reason="run record names no GitHub issue, so no candidate artifact can be joined",
            )
        )
        return
    candidates_dir = forge_dir / "intake" / "candidates"
    if not candidates_dir.is_dir():
        missing.append(
            MissingEvidence(
                kind=KIND_INTAKE_CANDIDATES,
                name=slug,
                reason="no .forge/intake/candidates directory in this project",
            )
        )
        return
    found = sorted(candidates_dir.glob(f"issue-{issue}-*.md"))
    if not found:
        missing.append(
            MissingEvidence(
                kind=KIND_INTAKE_CANDIDATES,
                name=slug,
                reason=f"no candidate artifact recorded for issue #{issue}",
            )
        )
        return
    for path in found:
        artifacts.append(_artifact(KIND_INTAKE_CANDIDATES, forge_dir, path, max_artifact_chars))


def _collect_runtime_identity(
    record: dict | None,
    slug: str,
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
) -> None:
    rows = invocation_identity_rows(record) if record is not None else []
    if rows:
        artifacts.append(
            EvidenceArtifact(
                kind=KIND_RUNTIME_IDENTITY,
                name=f"{slug}-runtime-identity.json",
                content=json.dumps(rows, indent=2, sort_keys=True, default=str),
                path=None,
            )
        )
        return
    missing.append(
        MissingEvidence(
            kind=KIND_RUNTIME_IDENTITY,
            name=slug,
            reason=(
                "run record carries no per-invocation identity rows"
                if record is not None
                else "no run record to read runtime identity from"
            ),
        )
    )


def _collect_resolved_configuration(
    record: dict | None,
    slug: str,
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
) -> None:
    block = (record or {}).get("configuration")
    if isinstance(block, dict) and block:
        artifacts.append(
            EvidenceArtifact(
                kind=KIND_RESOLVED_CONFIG,
                name=f"{slug}-resolved-configuration.json",
                content=json.dumps(block, indent=2, sort_keys=True, default=str),
                path=None,
            )
        )
        return
    missing.append(
        MissingEvidence(
            kind=KIND_RESOLVED_CONFIG,
            name=slug,
            reason=(
                "run record predates resolved-configuration capture"
                if record is not None
                else "no run record to read the resolved configuration from"
            ),
        )
    )


def _collect_sprint_state(
    forge_dir: Path,
    resolution: _Resolution,
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
    max_artifact_chars: int,
) -> None:
    paths: list[Path] = []
    if resolution.summary_path is not None:
        paths.append(resolution.summary_path)
    if resolution.sprint_audit_path is not None:
        paths.append(resolution.sprint_audit_path)
    if resolution.sprint_name:
        rolling = forge_dir / "logs" / resolution.sprint_name / "sprint-summary.yaml"
        if rolling.is_file():
            paths.append(rolling)
    if paths:
        for path in paths:
            artifacts.append(_artifact(KIND_SPRINT_STATE, forge_dir, path, max_artifact_chars))
        return
    missing.append(
        MissingEvidence(
            kind=KIND_SPRINT_STATE,
            reason=(
                "no sprint summary or sprint audit recorded for this run "
                "(standalone run, or the sprint artifacts were pruned)"
            ),
        )
    )


def _collect_sprint_run_log(
    forge_dir: Path,
    resolution: _Resolution,
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
    max_artifact_chars: int,
) -> None:
    """The sprint's own run log, distinct from each story's run log."""
    sprint_run_id = _sprint_log_id(resolution)
    if not sprint_run_id:
        return
    path = (forge_dir / "logs" / resolution.summary_path.parent.name) / f"run-{sprint_run_id}.log"
    if not path.is_file():
        missing.append(
            MissingEvidence(
                kind=KIND_RUN_LOG,
                name="sprint",
                reason=f"no .forge/logs/**/run-{sprint_run_id}.log in this project",
            )
        )
        return
    relative = _relpath(forge_dir, path)
    if any(a.path == relative for a in artifacts):
        return
    artifacts.append(_artifact(KIND_RUN_LOG, forge_dir, path, max_artifact_chars, tail=True))


def _sprint_log_id(resolution: _Resolution) -> str:
    if resolution.summary_path is None:
        return ""
    return resolution.summary_path.name[len("run-") : -len("-summary.yaml")]


# ── helpers ───────────────────────────────────────────────────────────────────


def _artifact(
    kind: str,
    forge_dir: Path,
    path: Path,
    max_chars: int,
    *,
    tail: bool = False,
) -> EvidenceArtifact:
    raw = path.read_text(encoding="utf-8", errors="replace")
    truncated_from: int | None = None
    if len(raw) > max_chars:
        truncated_from = len(raw)
        if tail:
            raw = (
                f"[truncated: kept the last {max_chars} of {truncated_from} characters]\n"
                + raw[-max_chars:]
            )
        else:
            raw = (
                f"[truncated: kept the first {max_chars} of {truncated_from} characters]\n"
                + raw[:max_chars]
            )
    return EvidenceArtifact(
        kind=kind,
        name=_relpath(forge_dir, path),
        content=raw,
        path=_relpath(forge_dir, path),
        truncated_from=truncated_from,
    )


def _relpath(forge_dir: Path, path: Path) -> str:
    try:
        return str(Path(".forge") / path.relative_to(forge_dir))
    except ValueError:
        return str(path)


def _summary_for_story(forge_dir: Path, sprint_name: str | None, run_id: str) -> Path | None:
    """Find the run-keyed sprint summary whose story rows name ``run_id``."""
    logs = forge_dir / "logs"
    if not logs.is_dir():
        return None
    pattern = f"{sprint_name}/run-*-summary.yaml" if sprint_name else "*/run-*-summary.yaml"
    for path in sorted(logs.glob(pattern)):
        data = _load_yaml(path)
        if not isinstance(data, dict):
            continue
        for row in data.get("stories") or []:
            if isinstance(row, dict) and _str_or_none(row.get("story_run_id")) == run_id:
                return path
    return None


def _find_sprint_summary(forge_dir: Path, run_id: str) -> Path | None:
    matches = sorted((forge_dir / "logs").glob(f"*/run-{run_id}-summary.yaml"))
    return matches[0] if matches else None


def _first_forge_version(records: dict[str, dict]) -> str | None:
    for record in records.values():
        version = _str_or_none(record.get("forge_version"))
        if version:
            return version
    return None


def _config_summary(
    records: dict[str, dict],
    artifacts: list[EvidenceArtifact],
    missing: list[MissingEvidence],
) -> str:
    captured = [a for a in artifacts if a.kind == KIND_RESOLVED_CONFIG]
    if not captured:
        reasons = {m.reason for m in missing if m.kind == KIND_RESOLVED_CONFIG}
        detail = "; ".join(sorted(reasons)) or "not recorded"
        return f"missing — {detail}"
    block: dict = {}
    for record in records.values():
        candidate = record.get("configuration")
        if isinstance(candidate, dict) and candidate:
            block = candidate
            break
    digest = _str_or_none(block.get("resolved_sha256")) or "unrecorded"
    entries = block.get("recorded_values")
    count = len((entries or {}).get("entries") or {}) if isinstance(entries, dict) else 0
    changed = block.get("changed_during_run")
    changed_text = (
        "changed during run"
        if changed is True
        else "unchanged during run"
        if changed is False
        else "mid-run change undetermined"
    )
    return (
        f"resolved snapshot attached ({count} recorded keys, "
        f"resolved sha256 {digest[:12]}, {changed_text})"
    )


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def _task_block(record: dict | None) -> dict:
    task = (record or {}).get("task")
    return task if isinstance(task, dict) else {}


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


_REMOTE_SECTION = re.compile(r'^\s*\[remote\s+"([^"]+)"\]\s*$')
_URL_LINE = re.compile(r"^\s*url\s*=\s*(.+?)\s*$")


def read_observed_project(project_root: Path) -> str | None:
    """Return ``owner/repo`` for the observing checkout, or ``None``.

    Read from the checkout's own git config rather than the network so the
    report can name where the behavior was seen without a GitHub round trip.
    """
    config_path = _git_config_path(project_root)
    if config_path is None:
        return None
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_origin = False
    url: str | None = None
    for line in text.splitlines():
        section = _REMOTE_SECTION.match(line)
        if section is not None:
            in_origin = section.group(1) == "origin"
            continue
        if line.lstrip().startswith("["):
            in_origin = False
            continue
        if in_origin:
            match = _URL_LINE.match(line)
            if match is not None:
                url = match.group(1)
    return _owner_repo(url) if url else None


def _git_config_path(project_root: Path) -> Path | None:
    git = project_root / ".git"
    if git.is_dir():
        return git / "config"
    if git.is_file():
        try:
            text = git.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("gitdir:"):
                gitdir = Path(line.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (project_root / gitdir).resolve()
                commondir = gitdir / "commondir"
                if commondir.is_file():
                    try:
                        common = Path(commondir.read_text(encoding="utf-8").strip())
                    except OSError:
                        common = Path()
                    if str(common):
                        if not common.is_absolute():
                            common = (gitdir / common).resolve()
                        return common / "config"
                return gitdir / "config"
    return None


def _owner_repo(url: str) -> str | None:
    cleaned = url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
        cleaned = cleaned.split("/", 1)[-1] if "/" in cleaned else ""
    elif ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    parts = [p for p in cleaned.split("/") if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


__all__ = [
    "EVIDENCE_KINDS",
    "EvidenceArtifact",
    "EvidenceError",
    "MAX_ARTIFACT_CHARS",
    "MissingEvidence",
    "RunEvidence",
    "collect_run_evidence",
    "read_observed_project",
]
