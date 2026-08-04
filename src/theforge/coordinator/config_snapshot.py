"""Sprint-scoped pinning of the operative ``forge.yaml``.

A sprint used to re-resolve its configuration from the project root on every
run/resume invocation (``run_setup._setup_resume_entry``). Any change to that
file mid-sprint — landed by a story that changes what a valid configuration is,
or made by the operator in the working tree — silently partitioned the sprint
into stories that ran under different contracts, with no artifact recording the
boundary (#1980).

This module holds the sprint's configuration identity: the content is copied
once, at sprint entry, into ``.forge/sprints/<sprint_id>/forge.yaml`` and every
story in that sprint is prepared from that copy. Re-entry (``--resume``, or the
``os.execv`` re-exec after a source update) reloads the existing snapshot rather
than recapturing, so the pin survives exactly the event that motivated it. When
the project-root file no longer matches the pin, the divergence is recorded as a
drift event on the snapshot record and surfaced in the sprint log and audit —
the story still runs under the pinned copy.

Stdlib + yaml only, so both the sprint runner and the coordinator's run-setup
path can import it without a dependency cycle.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: Points child processes (and the re-exec'd sprint process) at the pinned copy.
#: An environment variable rather than a call argument because the value has to
#: cross ``os.execv`` and the in-process story dispatch alike.
SNAPSHOT_ENV_VAR = "FORGE_SPRINT_CONFIG_SNAPSHOT"

_SNAPSHOT_FILENAME = "forge.yaml"
_RECORD_FILENAME = "config_snapshot.yaml"

# The process-wide active snapshot, set by the sprint runner at startup. One
# sprint per process, so a module global is the whole scope.
_ACTIVE: "SprintConfigSnapshot | None" = None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest_text(text: str) -> str:
    """Return the sha256 digest of ``text`` as used for config identity."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def snapshot_dir(project_root: Path, sprint_id: str) -> Path:
    return Path(project_root) / ".forge" / "sprints" / sprint_id


def snapshot_config_path(project_root: Path, sprint_id: str) -> Path:
    return snapshot_dir(project_root, sprint_id) / _SNAPSHOT_FILENAME


def snapshot_record_path(project_root: Path, sprint_id: str) -> Path:
    return snapshot_dir(project_root, sprint_id) / _RECORD_FILENAME


def project_config_path(project_root: Path) -> Path:
    return Path(project_root) / "forge.yaml"


def project_config_digest(project_root: Path) -> str | None:
    """Digest of the live project-root forge.yaml, or None when absent/unreadable."""
    text = _read_text(project_config_path(project_root))
    return None if text is None else digest_text(text)


@dataclass
class SprintConfigSnapshot:
    """The configuration identity a sprint runs under, and its drift history."""

    sprint_id: str
    project_root: Path
    digest: str | None
    captured_at: str
    source: str
    pinned_path: Path | None
    present: bool
    reused: bool = False
    drift_events: list[dict] = field(default_factory=list)

    def as_record(self) -> dict:
        return {
            "sprint_id": self.sprint_id,
            "digest": self.digest,
            "captured_at": self.captured_at,
            "source": self.source,
            "pinned_path": str(self.pinned_path) if self.pinned_path else None,
            "present": self.present,
            "drift_events": list(self.drift_events),
        }

    def save(self) -> None:
        """Persist the record next to the pinned copy. Best effort."""
        path = snapshot_record_path(self.project_root, self.sprint_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump(self.as_record(), fh, default_flow_style=False, sort_keys=False)
        except OSError:
            pass


def _load_record(project_root: Path, sprint_id: str) -> dict | None:
    path = snapshot_record_path(project_root, sprint_id)
    text = _read_text(path)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def load_audit_record(project_root: Path, sprint_id: str | None) -> dict | None:
    """Return the persisted snapshot record for the sprint audit, if any."""
    if not sprint_id:
        return None
    return _load_record(Path(project_root), sprint_id)


def capture_or_load(project_root: Path, sprint_id: str) -> SprintConfigSnapshot:
    """Return this sprint's pinned configuration, capturing it on first entry.

    Capture happens once per logical sprint. On re-entry — ``--resume`` or the
    re-exec that follows a source update — the existing snapshot is reloaded
    even if the project root has since changed, which is precisely the case the
    pin exists for; the difference is reported by :func:`check_drift`, not
    absorbed by a fresh capture.

    A missing or unreadable project-root ``forge.yaml`` yields a snapshot with
    ``present=False`` rather than an error: callers fall back to their previous
    behaviour of reading the project root directly.
    """
    project_root = Path(project_root)
    pinned = snapshot_config_path(project_root, sprint_id)
    record = _load_record(project_root, sprint_id)

    if record is not None and pinned.exists():
        return SprintConfigSnapshot(
            sprint_id=sprint_id,
            project_root=project_root,
            digest=record.get("digest"),
            captured_at=str(record.get("captured_at") or ""),
            source=str(record.get("source") or str(project_config_path(project_root))),
            pinned_path=pinned,
            present=True,
            reused=True,
            drift_events=list(record.get("drift_events") or []),
        )

    src = project_config_path(project_root)
    text = _read_text(src)
    if text is None:
        return SprintConfigSnapshot(
            sprint_id=sprint_id,
            project_root=project_root,
            digest=None,
            captured_at=_now(),
            source=str(src),
            pinned_path=None,
            present=False,
        )

    snapshot = SprintConfigSnapshot(
        sprint_id=sprint_id,
        project_root=project_root,
        digest=digest_text(text),
        captured_at=_now(),
        source=str(src),
        pinned_path=pinned,
        present=True,
    )
    try:
        pinned.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, pinned)
    except OSError:
        # Disk full, permissions, a read-only .forge — the sprint still runs, it
        # just runs unpinned, exactly as it did before this mechanism existed.
        snapshot.pinned_path = None
        snapshot.present = False
        return snapshot
    snapshot.save()
    return snapshot


def check_drift(
    snapshot: "SprintConfigSnapshot | None",
    *,
    story: str | None = None,
) -> dict | None:
    """Record and return a drift event when the project root has moved off the pin.

    Returns None when there is nothing to report (no snapshot, no pin, or the
    live file still matches). A repeat of an already-recorded digest for the
    same story is not re-recorded, so a parallel sprint does not fill the record
    with duplicates of one edit.
    """
    if snapshot is None or not snapshot.present or snapshot.digest is None:
        return None
    current = project_config_digest(snapshot.project_root)
    if current == snapshot.digest:
        return None
    for prior in snapshot.drift_events:
        if prior.get("project_root_digest") == current and prior.get("story") == story:
            return None
    event = {
        "detected_at": _now(),
        "story": story,
        "pinned_digest": snapshot.digest,
        "project_root_digest": current,
        "project_root_config_present": current is not None,
        "pinned_config_in_effect": True,
    }
    snapshot.drift_events.append(event)
    snapshot.save()
    return event


def describe_drift(event: dict) -> str:
    """One operator-facing line for a drift event."""
    where = f" before story {event['story']}" if event.get("story") else ""
    if not event.get("project_root_config_present", True):
        current = "removed"
    else:
        current = f"now {str(event.get('project_root_digest'))[:12]}"
    return (
        f"forge.yaml in the project root changed after this sprint pinned its config{where} "
        f"(pinned {str(event.get('pinned_digest'))[:12]}, {current}); "
        "stories keep running under the pinned snapshot"
    )


def activate(snapshot: "SprintConfigSnapshot | None") -> None:
    """Make ``snapshot`` the config source for stories dispatched by this process."""
    global _ACTIVE
    _ACTIVE = snapshot
    if snapshot is not None and snapshot.present and snapshot.pinned_path is not None:
        os.environ[SNAPSHOT_ENV_VAR] = str(snapshot.pinned_path)
    else:
        os.environ.pop(SNAPSHOT_ENV_VAR, None)


def deactivate() -> None:
    """Drop the active pin (end of sprint, or tests)."""
    global _ACTIVE
    _ACTIVE = None
    os.environ.pop(SNAPSHOT_ENV_VAR, None)


def active_snapshot() -> "SprintConfigSnapshot | None":
    return _ACTIVE


def pinned_forge_yaml() -> Path | None:
    """The pinned forge.yaml to prepare worktrees from, or None for standalone runs.

    Read from the environment so it survives ``os.execv`` and reaches child
    processes; a pin whose file has disappeared is treated as absent, which
    returns the caller to the project-root source.
    """
    raw = os.environ.get(SNAPSHOT_ENV_VAR)
    if not raw:
        return None
    path = Path(raw)
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    return path
