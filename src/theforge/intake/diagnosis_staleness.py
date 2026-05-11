"""Diagnosis-baseline staleness detection for ``forge groom``.

ADR-0001's three-state diagnosis vocabulary records what the operator knew
at write time but is silent about how that knowledge ages. ``forge diagnose``
anchors a diagnosis to a baseline SHA and the content hash of every file
it inspected; ``forge groom`` uses this module to ask the mechanical
question "have any inspected files changed since the baseline?" so a stale
diagnosis cannot silently transition the issue to ``ready``.

Stdlib only — no provider SDK or runner dependencies.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from theforge.diagnose_types import (
    BaselineMetadata,
    InspectedFile,
    parse_baseline_metadata,
)


class StalenessState(str, Enum):
    """Outcome of comparing a diagnosis baseline against the current base."""

    NOT_APPLICABLE = "not-applicable"  # not a bug, or no diagnosis section
    NO_BASELINE = "no-baseline"  # diagnosis predates baseline anchoring
    FRESH = "fresh"  # baseline equals current HEAD (no advance)
    UNCHANGED = "unchanged"  # base advanced but inspected files unchanged
    STALE = "stale"  # base advanced AND inspected files changed materially


@dataclass(frozen=True)
class StalenessReport:
    """Result of a staleness check against current base."""

    state: StalenessState
    baseline_sha: str = ""
    baseline_captured_at: str = ""
    current_sha: str = ""
    changed_files: tuple[str, ...] = field(default_factory=tuple)
    unchanged_files: tuple[str, ...] = field(default_factory=tuple)
    untracked_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_stale(self) -> bool:
        return self.state is StalenessState.STALE


def _current_head(project_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _hash_at_sha(
    path: str, sha: str, project_root: Path
) -> tuple[str, Literal["present", "absent", "unknown"]]:
    """Return ``(hex_digest, presence)`` for ``path`` at git ``sha``.

    Presence is ``"present"`` when git successfully returned the blob,
    ``"absent"`` when git confirmed the path does not exist at that commit
    (a positive deletion signal — treated as a material change by callers),
    and ``"unknown"`` when git itself is unavailable / errored / there is no
    sha to query, in which case callers should not flip the staleness verdict.
    Falls back to the working tree only when git lookup is inconclusive.
    """
    if sha:
        try:
            proc = subprocess.run(
                ["git", "cat-file", "-e", f"{sha}:{path}"],
                capture_output=True,
                cwd=str(project_root),
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None:
            if proc.returncode == 0:
                try:
                    show = subprocess.run(
                        ["git", "show", f"{sha}:{path}"],
                        capture_output=True,
                        cwd=str(project_root),
                        timeout=10,
                    )
                except (OSError, subprocess.SubprocessError):
                    show = None
                if show is not None and show.returncode == 0:
                    return hashlib.sha256(show.stdout).hexdigest(), "present"
            else:
                # git is reachable and the path is positively absent at sha.
                return "", "absent"
    fs_path = project_root / path
    try:
        return hashlib.sha256(fs_path.read_bytes()).hexdigest(), "present"
    except FileNotFoundError:
        # No git answer and no working-tree file — treat as a real absence
        # so deletions surface as material changes.
        return "", "absent"
    except OSError:
        return "", "unknown"


def evaluate_staleness(
    body: str,
    *,
    project_root: Path | None,
) -> StalenessReport:
    """Return a :class:`StalenessReport` for the diagnosis section in ``body``.

    Parses ``**Baseline:**`` + ``**Inspected files:**`` metadata out of the
    body, asks git for the current HEAD, then re-hashes each inspected file
    against the working tree.

    Without a usable ``project_root`` (no git checkout, or no inspected
    files) the report is :attr:`StalenessState.NO_BASELINE` /
    :attr:`StalenessState.NOT_APPLICABLE` / :attr:`StalenessState.UNCHANGED`
    rather than ``STALE`` — the staleness gate is a refusal mechanism, so
    only positive evidence of drift produces a refusal verdict.
    """
    metadata = parse_baseline_metadata(body)
    if metadata is None:
        return StalenessReport(state=StalenessState.NO_BASELINE)

    if project_root is None:
        # Without a checkout we cannot compute current hashes; report the
        # baseline back to callers as informational state only.
        return StalenessReport(
            state=StalenessState.UNCHANGED,
            baseline_sha=metadata.baseline_sha,
            baseline_captured_at=metadata.baseline_captured_at,
        )

    current = _current_head(project_root)
    if current and current == metadata.baseline_sha:
        # Base hasn't advanced — by definition, no material drift.
        return StalenessReport(
            state=StalenessState.FRESH,
            baseline_sha=metadata.baseline_sha,
            baseline_captured_at=metadata.baseline_captured_at,
            current_sha=current,
            unchanged_files=tuple(f.path for f in metadata.inspected_files),
        )

    return _diff_inspected(metadata, current, project_root)


def _diff_inspected(
    metadata: BaselineMetadata, current_sha: str, project_root: Path
) -> StalenessReport:
    changed: list[str] = []
    unchanged: list[str] = []
    untracked: list[str] = []
    for f in metadata.inspected_files:
        baseline_digest = (f.content_sha256 or "").lower()
        current_digest, presence = _hash_at_sha(f.path, current_sha, project_root)
        # Deletion is a material change: a path with a baseline digest that
        # is positively absent at the current SHA must surface as STALE,
        # not get hidden in untracked_files (which would let groom promote
        # a confirmed-cause diagnosis whose evidence has been removed).
        if baseline_digest and presence == "absent":
            changed.append(f.path)
            continue
        if not baseline_digest or not current_digest:
            untracked.append(f.path)
            continue
        if current_digest.lower() != baseline_digest:
            changed.append(f.path)
        else:
            unchanged.append(f.path)
    state = StalenessState.STALE if changed else StalenessState.UNCHANGED
    return StalenessReport(
        state=state,
        baseline_sha=metadata.baseline_sha,
        baseline_captured_at=metadata.baseline_captured_at,
        current_sha=current_sha,
        changed_files=tuple(changed),
        unchanged_files=tuple(unchanged),
        untracked_files=tuple(untracked),
    )


__all__ = [
    "StalenessState",
    "StalenessReport",
    "evaluate_staleness",
    "InspectedFile",
]
