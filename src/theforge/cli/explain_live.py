"""Locate a routing record for a story that has not finished (#2923).

``forge explain`` reads the SQLite audit substrate, and a story only enters the
substrate when it reaches a terminal outcome: the per-story write path is the
one that upserts the record. A story that is *still running*, or that
``forge stop`` killed mid-flight, therefore has no substrate row — while the
routing decision the operator is asking about (candidate pool, exclusion
reasons, tie-break, promotion/demotion checks) has already been computed and
written durably to two other stores:

``.forge/logs/<sprint>/<slug>/audit.yaml``
    The audit the sprint process flushes as the story runs, marked
    ``in_flight: true`` and stamped terminal by ``forge stop``
    (:func:`theforge.sprint.audit.write_live_story_audit` /
    :func:`~theforge.sprint.audit.finalize_interrupted_story_audit`). A single
    story run (``forge run``) writes the same file one directory shallower, at
    ``.forge/logs/<slug>/audit.yaml``. This is a full audit record — same shape,
    same ``routing_decision`` key, as the record the substrate would hold.

``.forge/resume_state/<slug>.json``
    The resume record, which carries ``routing_decision`` as a named restore
    block (:mod:`theforge.coordinator.resume_persistence`). It is *not* a full
    audit record — it holds phase blocks only, so it carries no configuration
    provenance.

This module finds the freshest of those, and reports which store it came from
so the caller can say so rather than presenting an unfinished record as a
finished one. It reads only; nothing here writes, migrates, or rebuilds.

The need it serves is the operator's: the question "why was this story routed
here" is asked most often while the run is unfinished, which is exactly when the
substrate cannot answer it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from theforge.coordinator import resume_persistence

#: Store identifiers, ordered from most to least complete as an explanation.
STORE_IN_FLIGHT_AUDIT = "in_flight_audit"
STORE_INTERRUPTED_AUDIT = "interrupted_audit"
STORE_UNPUBLISHED_AUDIT = "unpublished_audit"
STORE_RESUME_STATE = "resume_state"

#: Operator-facing name for each store, used in the provenance line.
STORE_LABELS = {
    STORE_IN_FLIGHT_AUDIT: "in-flight story audit",
    STORE_INTERRUPTED_AUDIT: "interrupted story audit",
    STORE_UNPUBLISHED_AUDIT: "per-story audit (not yet in the substrate)",
    STORE_RESUME_STATE: "resume record",
}

#: Stores that hold a full audit record, so an absent configuration block means
#: "this run predates configuration provenance" rather than "this store does not
#: carry one". The resume record is the exception.
_FULL_AUDIT_STORES = frozenset(
    {STORE_IN_FLIGHT_AUDIT, STORE_INTERRUPTED_AUDIT, STORE_UNPUBLISHED_AUDIT}
)

_IN_FLIGHT_KEY = "in_flight"


@dataclass(frozen=True)
class LiveRecord:
    """One routing record found outside the substrate."""

    record: dict
    path: Path
    store: str
    mtime_ns: int

    @property
    def label(self) -> str:
        return STORE_LABELS.get(self.store, self.store)

    @property
    def carries_configuration(self) -> bool:
        """Whether an absent ``configuration`` block is meaningful for this store."""
        return self.store in _FULL_AUDIT_STORES

    @property
    def has_routing_decision(self) -> bool:
        return isinstance(self.record.get("routing_decision"), dict) and bool(
            self.record["routing_decision"]
        )


@dataclass(frozen=True)
class LiveLookup:
    """Outcome of a search across the non-substrate stores.

    ``found`` is the freshest matching record, or ``None``.

    An unparseable file is reported in one of two places, and the difference is
    the whole point of splitting them. ``unreadable`` holds ``(path, error)``
    for files whose *location* identifies the story that was asked about: that
    story has a record and it could not be read, which is a different answer to
    the operator than "no record exists". ``unattributed`` holds unparseable
    files that could not be tied to the target either way — a ``--run`` lookup
    has nothing in a path to match against, so a corrupt audit belonging to some
    other story must never be reported as the requested run's record.
    """

    found: LiveRecord | None
    unreadable: tuple[tuple[Path, str], ...]
    searched: tuple[str, ...]
    unattributed: tuple[tuple[Path, str], ...] = ()


def logs_root(project_root: Path) -> Path:
    return Path(project_root) / ".forge" / "logs"


def _story_audit_paths(project_root: Path) -> list[Path]:
    """Every per-story ``audit.yaml`` on disk, sprint-nested or single-run."""
    root = logs_root(project_root)
    if not root.exists():
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in ("*/audit.yaml", "*/*/audit.yaml"):
        for path in root.glob(pattern):
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _record_slug(record: dict) -> str | None:
    task = record.get("task")
    if isinstance(task, dict):
        slug = task.get("slug")
        if isinstance(slug, str) and slug:
            return slug
    slug = record.get("slug")
    return slug if isinstance(slug, str) and slug else None


def _record_issue(record: dict) -> int | None:
    task = record.get("task")
    if isinstance(task, dict):
        issue = task.get("github_issue")
        if isinstance(issue, int):
            return issue
    return None


def _matches(
    record: dict,
    path: Path,
    *,
    run_id: str | None,
    slug: str | None,
    issue_id: int | None,
) -> bool:
    if run_id is not None:
        return record.get("run_id") == run_id
    if slug is not None:
        # The directory name is the story slug for both layouts, and is the only
        # signal a record written before the slug was stamped into it carries.
        return _record_slug(record) == slug or path.parent.name == slug
    if issue_id is not None:
        recorded = _record_issue(record)
        if recorded is not None:
            return recorded == issue_id
        # The resume record holds no issue number, so fall back to the slug
        # convention `forge` itself uses when it derives a slug from an issue.
        conventional = f"issue-{issue_id}"
        return _record_slug(record) == conventional or path.parent.name == conventional
    return False


def _path_identifies_target(identity: str, *, slug: str | None, issue_id: int | None) -> bool:
    """Whether a file's location alone ties it to the story that was asked about.

    ``identity`` is the story name the path carries: the story directory for a
    per-story audit, the filename stem for a resume record. A ``run_id`` lookup
    gets ``False`` for everything — no path names a run — so an unparseable file
    is never attributed to a run on the strength of merely existing.
    """
    if slug is not None:
        return identity == slug
    if issue_id is not None:
        return identity == f"issue-{issue_id}"
    return False


def _audit_store(record: dict) -> str:
    if record.get(_IN_FLIGHT_KEY):
        return STORE_IN_FLIGHT_AUDIT
    if record.get("interrupted_by"):
        return STORE_INTERRUPTED_AUDIT
    return STORE_UNPUBLISHED_AUDIT


def _load(path: Path, *, as_json: bool) -> tuple[dict | None, str | None]:
    """Parse one record file; return ``(record, error)`` with exactly one set."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if as_json else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, f"not a mapping ({type(data).__name__})"
    return data, None


def find_live_record(
    project_root: Path,
    *,
    run_id: str | None = None,
    slug: str | None = None,
    issue_id: int | None = None,
) -> LiveLookup:
    """Find the freshest routing record outside the substrate for one story/run.

    Exactly one of ``run_id`` / ``slug`` / ``issue_id`` selects the record,
    mirroring :func:`audit_substrate.latest_record_for`.

    Freshness is file mtime, and a record that actually carries a
    ``routing_decision`` always outranks one that does not — a resume record
    rewritten a second ago with no routing block yet must not displace the
    in-flight audit that holds the decision the operator asked about.
    """
    project_root = Path(project_root)
    candidates: list[LiveRecord] = []
    unreadable: list[tuple[Path, str]] = []
    unattributed: list[tuple[Path, str]] = []

    for path in _story_audit_paths(project_root):
        record, error = _load(path, as_json=False)
        if record is None:
            # A file that will not parse cannot say whose it is, so its location
            # is the only evidence left. Attributed to the target only when the
            # story directory names it; otherwise recorded as unattributed, so a
            # corrupt audit for another story is never reported as this one's.
            bucket = (
                unreadable
                if _path_identifies_target(path.parent.name, slug=slug, issue_id=issue_id)
                else unattributed
            )
            bucket.append((path, error or "unreadable"))
            continue
        if not _matches(record, path, run_id=run_id, slug=slug, issue_id=issue_id):
            continue
        candidates.append(
            LiveRecord(
                record=record,
                path=path,
                store=_audit_store(record),
                mtime_ns=_mtime_ns(path),
            )
        )

    for path in _resume_record_paths(project_root, slug=slug):
        record, error = _load(path, as_json=True)
        if record is None:
            # A slug lookup reads exactly one resume record, addressed by the
            # slug itself, so that file is the target's by construction.
            attributed = slug is not None or _path_identifies_target(
                path.stem, slug=slug, issue_id=issue_id
            )
            (unreadable if attributed else unattributed).append((path, error or "unreadable"))
            continue
        if not _matches(record, path, run_id=run_id, slug=slug, issue_id=issue_id):
            continue
        candidates.append(
            LiveRecord(
                record=record,
                path=path,
                store=STORE_RESUME_STATE,
                mtime_ns=_mtime_ns(path),
            )
        )

    searched = (
        str(logs_root(project_root) / "**" / "audit.yaml"),
        str(resume_persistence.resume_records_dir(project_root) / "<slug>.json"),
    )
    best = (
        max(candidates, key=lambda c: (c.has_routing_decision, c.mtime_ns)) if candidates else None
    )
    return LiveLookup(
        found=best,
        unreadable=tuple(unreadable),
        searched=searched,
        unattributed=tuple(unattributed),
    )


def _resume_record_paths(project_root: Path, *, slug: str | None) -> list[Path]:
    """Resume records to consider: the named one when a slug is known, else all."""
    if slug is not None:
        path = resume_persistence.resume_record_path(project_root, slug)
        return [path] if path.exists() else []
    directory = resume_persistence.resume_records_dir(project_root)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def migrate_if_versioned(record: dict) -> dict:
    """Bring a record up to the current schema when it declares an old version.

    Mirrors the ``--file`` path so a live record renders identically to the same
    record read back after publication. A record whose migration fails is
    returned untouched: a partially explained decision beats none.
    """
    from theforge.coordinator import audit_substrate  # noqa: PLC0415

    version = record.get("schema_version")
    if not isinstance(version, int):
        return record
    try:
        migrated = audit_substrate._migrate_record(record, from_version=version)
    except Exception:  # noqa: BLE001 — a reader must not fail on a bad old record
        return record
    return migrated if isinstance(migrated, dict) else record
