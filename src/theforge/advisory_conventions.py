"""Rolling aggregation and surfacing for advisory convention violations.

The rolling artifact is a **shared mutable path**: every story in a sprint
resolves it against the same project root, so a ``--parallel`` sprint has
several workers reading, merging, and rewriting one file. Two consequences are
handled here rather than left to luck (#2107):

* the read-merge-write is serialized under a per-destination lock, and the
  scratch file each writer stages through is unique, so concurrent writers can
  neither collide on a scratch path nor overwrite each other's observations;
* a persistence failure raises :class:`AdvisoryArtifactError`, which names the
  artifact rather than the story that happened to be executing — this is shared
  run infrastructure, not a statement about anyone's work.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from .config import ForgeConfig

_LINE_COUNT_RULES = frozenset({"max_module_lines", "max_test_file_lines"})
_DETAIL_RE = re.compile(r"^(?P<file>.+) has (?P<line_count>\d+) lines \(limit (?P<limit>\d+)\)$")

# Shape-gate-compatible runnable type label. Auto-filed findings must carry
# exactly one recognized type label or sprint intake refuses to dispatch them
# — see shape_check.heuristics.check_missing_type. ``task`` is the appropriate
# type for an operator-driven refactor of an oversized module; the rendered
# body is task-shaped (Summary + AC), so any other type label (``bug``,
# ``enhancement``, ``epic``) would either conflict with ``task`` (multiple
# type labels → missing_type BLOCKING) or demand a body shape we do not
# render (e.g. ``bug`` requires a complete Diagnosis section). Recognized
# type labels supplied via ``issue_filing.label`` are therefore dropped from
# the gh invocation; the resulting issue carries only ``task``.
_RUNNABLE_TYPE_LABEL = "task"
_RECOGNIZED_TYPE_LABELS = frozenset({"bug", "enhancement", "epic", "task"})

_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

# How long a persisted entry that the *current* scan did not observe is kept.
#
# A scan states what one worker saw in one worktree; it is not authority to
# delete what another worker recorded. Dropping every unobserved entry (the
# pre-#2107 behavior) means the last story of a parallel sprint erases the
# observations of the stories that ran beside it, silently and with no error.
# The retention window is what makes "unobserved" mean *stale* rather than
# merely *not mine*: it must comfortably exceed the wall-clock spread of the
# stories in one sprint, so no worker can outlive its peers' observations. A
# genuinely fixed file therefore lingers for at most this long, carrying the
# ``last_seen`` that says so.
_UNOBSERVED_ENTRY_RETENTION_SECONDS = 24 * 60 * 60


class AdvisoryArtifactError(RuntimeError):
    """Persisting the rolling advisory artifact failed.

    Raised instead of the bare ``OSError`` so callers can tell a failure of
    *shared run infrastructure* — a path every story in a sprint writes — apart
    from a failure of the story that happened to be executing. Carries the
    artifact path and chains the original exception so the operator-facing
    record keeps the real errno and both paths (#2107).
    """

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"advisory artifact persistence failed for {path}: {cause}")
        self.path = Path(path)
        self.cause = cause


def advisory_artifact_path(config: ForgeConfig) -> Path:
    """Return the local rolling advisory artifact path."""
    return config.project_root / config.conventions_advisory.artifact_path


def load_advisory_summary(config: ForgeConfig) -> dict[str, Any]:
    """Load the local rolling advisory artifact, returning an empty structure on failure."""
    artifact_path = advisory_artifact_path(config)
    if not artifact_path.exists():
        return {"entries": {}}
    try:
        with open(artifact_path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {"entries": {}}
    if not isinstance(data, dict):
        return {"entries": {}}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        data["entries"] = {}
    return data


def noteworthy_advisory_entries(config: ForgeConfig) -> list[dict[str, Any]]:
    """Return current notable advisory entries sorted by largest absolute gap."""
    entries = list(load_advisory_summary(config).get("entries", {}).values())
    threshold = config.conventions_advisory.noteworthy_threshold_percent
    notable = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and _entry_percent_over(entry) is not None
        and _entry_percent_over(entry) >= threshold
    ]
    notable.sort(
        key=lambda entry: (
            int(entry.get("gap") or 0),
            float(_entry_percent_over(entry) or 0.0),
            str(entry.get("file") or ""),
        ),
        reverse=True,
    )
    return notable[: config.conventions_advisory.summary_top_n]


def update_advisory_violations(
    config: ForgeConfig,
    violations: list[dict[str, Any]],
    *,
    observed_at: dt.datetime,
    run_id: str | None,
    story_slug: str | None,
) -> dict[str, Any]:
    """Update the rolling artifact from the current advisory convention scan.

    Concurrency-safe by construction (#2107): the read-merge-write is performed
    under an exclusive lock on the destination artifact, so a worker always
    merges against what its peers have already persisted rather than against a
    snapshot taken before they wrote. Issue filing — which shells out to ``gh``
    — deliberately runs *outside* the lock, and the filed issue block is merged
    back under a second, short acquisition; holding the artifact lock across a
    network round-trip would queue every other story behind GitHub.

    Raises:
        AdvisoryArtifactError: the artifact could not be persisted.
    """
    artifact_path = advisory_artifact_path(config)
    observed_at_str = observed_at.astimezone(dt.timezone.utc).strftime(_TIMESTAMP_FMT)

    normalized = [
        norm
        for norm in (_normalize_advisory_violation(violation) for violation in violations)
        if norm is not None
    ]

    with _artifact_lock(config, artifact_path):
        merged = _merge_observations(
            load_advisory_summary(config),
            normalized,
            observed_at=observed_at,
            observed_at_str=observed_at_str,
            run_id=run_id,
            story_slug=story_slug,
        )
        _persist_artifact_data(config, merged)

    observed_keys = [_entry_key(norm["rule"], norm["file"]) for norm in normalized]
    observed_entries = {key: merged["entries"][key] for key in observed_keys}

    newly_filed: list[dict[str, Any]] = []
    for key, entry in observed_entries.items():
        maybe_issue = _maybe_file_issue(config, entry)
        if maybe_issue is not None:
            entry["issue"] = maybe_issue
            newly_filed.append({"key": key, **maybe_issue})

    if newly_filed:
        with _artifact_lock(config, artifact_path):
            merged = load_advisory_summary(config)
            entries = merged.setdefault("entries", {})
            for filed in newly_filed:
                key = filed["key"]
                current = entries.get(key)
                entries[key] = {
                    **(current if isinstance(current, dict) else observed_entries[key]),
                    "issue": {k: v for k, v in filed.items() if k != "key"},
                }
            merged["updated_at"] = observed_at_str
            _persist_artifact_data(config, merged)

    return {
        "path": str(artifact_path),
        "entry_count": len(merged.get("entries", {})),
        "newly_filed_issues": newly_filed,
        "entries": observed_entries,
    }


def _merge_observations(
    existing: dict[str, Any],
    normalized: list[dict[str, Any]],
    *,
    observed_at: dt.datetime,
    observed_at_str: str,
    run_id: str | None,
    story_slug: str | None,
) -> dict[str, Any]:
    """Fold this scan's observations into the persisted artifact.

    Observed entries win. Persisted entries this scan did not observe are kept
    while they are fresh (see ``_UNOBSERVED_ENTRY_RETENTION_SECONDS``) and
    pruned once stale, so a concurrent worker's just-recorded observation is
    never deleted by a peer that did not see it.
    """
    existing_entries = existing.get("entries", {})
    if not isinstance(existing_entries, dict):
        existing_entries = {}

    observed: dict[str, dict[str, Any]] = {}
    for norm in normalized:
        key = _entry_key(norm["rule"], norm["file"])
        prior = existing_entries.get(key) if isinstance(existing_entries.get(key), dict) else None
        entry = {
            "rule": norm["rule"],
            "file": norm["file"],
            "detail": norm["detail"],
            "line_count": norm["line_count"],
            "limit": norm["limit"],
            "gap": norm["gap"],
            "first_seen": (prior or {}).get("first_seen") or observed_at_str,
            "last_seen": observed_at_str,
            "last_run_id": run_id,
            "last_story_slug": story_slug,
        }
        issue_block = (
            dict(prior.get("issue", {})) if prior and isinstance(prior.get("issue"), dict) else {}
        )
        if issue_block:
            entry["issue"] = issue_block
        observed[key] = entry

    entries: dict[str, dict[str, Any]] = {}
    for key, prior in existing_entries.items():
        if key in observed:
            entries[key] = observed[key]
        elif isinstance(prior, dict) and _entry_is_fresh(prior, observed_at):
            entries[key] = prior
    for key, entry in observed.items():
        entries.setdefault(key, entry)

    return {"updated_at": observed_at_str, "entries": entries}


def _entry_is_fresh(entry: dict[str, Any], observed_at: dt.datetime) -> bool:
    """True when *entry* was last seen recently enough to survive this scan.

    An entry whose ``last_seen`` is missing or unparseable cannot be shown to be
    another writer's live observation, so it is treated as stale — the
    pre-existing pruning behavior for malformed entries.
    """
    raw = entry.get("last_seen")
    if not isinstance(raw, str):
        return False
    try:
        last_seen = dt.datetime.strptime(raw, _TIMESTAMP_FMT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    age = (observed_at.astimezone(dt.timezone.utc) - last_seen).total_seconds()
    return age < _UNOBSERVED_ENTRY_RETENTION_SECONDS


def _persist_artifact_data(config: ForgeConfig, artifact_data: dict[str, Any]) -> None:
    """Write the artifact (and its optional committed mirror) atomically."""
    advisory_cfg = config.conventions_advisory
    _write_yaml_atomic(advisory_artifact_path(config), artifact_data)
    if advisory_cfg.commit_shared_artifact and advisory_cfg.shared_artifact_path:
        shared_path = config.project_root / advisory_cfg.shared_artifact_path
        with _artifact_lock(config, shared_path):
            _write_yaml_atomic(shared_path, artifact_data)


def _normalize_advisory_violation(violation: dict[str, Any]) -> dict[str, Any] | None:
    rule = violation.get("rule")
    file = violation.get("file")
    detail = violation.get("detail")
    blocking = violation.get("blocking", True)
    if blocking or rule not in _LINE_COUNT_RULES:
        return None
    if not isinstance(rule, str) or not isinstance(file, str) or not isinstance(detail, str):
        return None
    parsed = _DETAIL_RE.match(detail.strip())
    if parsed is None:
        return None
    line_count = int(parsed.group("line_count"))
    limit = int(parsed.group("limit"))
    return {
        "rule": rule,
        "file": file,
        "detail": detail,
        "line_count": line_count,
        "limit": limit,
        "gap": max(0, line_count - limit),
    }


def _entry_key(rule: str, file: str) -> str:
    return json.dumps([rule, file], separators=(",", ":"))


def _entry_percent_over(entry: dict[str, Any]) -> float | None:
    try:
        gap = float(entry["gap"])
        limit = float(entry["limit"])
    except (KeyError, TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    return (gap / limit) * 100.0


def _resolve_issue_labels(configured_label: str) -> list[str]:
    """Return the labels to attach to an auto-filed advisory issue.

    Always includes the runnable type label. The configured filing label is
    appended only when it is not itself a recognized type label — a
    second type label would either conflict with ``task`` (sprint intake's
    ``check_missing_type`` rejects multi-type issues) or demand a body
    shape this module does not render (``bug`` requires Diagnosis). Dropping
    the conflicting label is preferred over refusing to file: the audit
    trail still attributes the issue to the advisory finding via title and
    body, and the operator can re-label after triage.
    """
    labels: list[str] = [_RUNNABLE_TYPE_LABEL]
    normalized = configured_label.strip()
    if normalized and normalized.lower() not in _RECOGNIZED_TYPE_LABELS:
        labels.append(normalized)
    return labels


def _render_issue_body(entry: dict[str, Any], percent_over: float) -> str:
    """Render a sprint-runnable issue body for an advisory convention finding.

    The body conforms to the ``task``-shape contract enforced by
    ``shape_check``: a non-empty ``## Acceptance criteria`` section whose
    bullets contain observable verbs, no ``## Design`` heading, and an
    ``Implementation target:`` line that opts the body out of the
    file-path-based implementation-plan check.
    """
    rule = entry["rule"]
    file = entry["file"]
    line_count = entry["line_count"]
    limit = entry["limit"]
    gap = entry["gap"]
    pct = f"{percent_over:.0f}%"
    audit_yaml = yaml.safe_dump(
        {
            "rule": rule,
            "file": file,
            "detail": entry["detail"],
            "line_count": line_count,
            "limit": limit,
            "gap": gap,
            "blocking": False,
            "last_run_id": entry.get("last_run_id"),
            "last_story_slug": entry.get("last_story_slug"),
        },
        sort_keys=False,
    ).strip()

    lines = [
        "## Summary",
        "",
        (
            f"Advisory convention `{rule}` is exceeded by `{file}`: "
            f"{line_count} lines vs the configured limit of {limit} "
            f"(gap of {gap} lines, {pct} over). Refactor the module to bring "
            "it under the limit by extracting cohesive subunits into sibling "
            "modules without changing observable behavior."
        ),
        "",
        f"Implementation target: {file}",
        "",
        "## Acceptance criteria",
        "",
        (
            f"- Re-running the convention check reports `{file}` at no more "
            f"than {limit} lines (currently {line_count})."
        ),
        "- `make gate` passes after the refactor.",
        (
            f"- Existing imports of symbols defined in `{file}` continue to "
            "resolve; no public name is removed without a re-export."
        ),
        "",
        "## Audit excerpt",
        "",
        "```yaml",
        audit_yaml,
        "```",
    ]
    return "\n".join(lines)


def _maybe_file_issue(config: ForgeConfig, entry: dict[str, Any]) -> dict[str, Any] | None:
    issue_cfg = config.conventions_advisory.issue_filing
    if not issue_cfg.enabled:
        return None
    if isinstance(entry.get("issue"), dict):
        return entry["issue"]
    percent_over = _entry_percent_over(entry)
    if percent_over is None or percent_over < issue_cfg.threshold_percent:
        return None

    title = f"Advisory convention debt: {entry['rule']} in {entry['file']}"
    body = _render_issue_body(entry, percent_over)
    labels = _resolve_issue_labels(issue_cfg.label)
    command = ["gh", "issue", "create", "--title", title, "--body", body]
    for label in labels:
        command.extend(["--label", label])
    if issue_cfg.milestone:
        command.extend(["--milestone", issue_cfg.milestone])
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=str(config.project_root),
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return None
    issue_url = proc.stdout.strip() or None
    return {
        "title": title,
        "url": issue_url,
        "label": issue_cfg.label,
        "labels_applied": labels,
        "milestone": issue_cfg.milestone,
        "filed_at": entry["last_seen"],
    }


def _lock_path(config: ForgeConfig, path: Path) -> Path:
    """Lock file for the artifact at *path*, under the project's lock directory.

    Kept under ``.forge/locks/`` rather than beside the artifact because the
    optional committed mirror lives in the working tree: a sidecar there would
    leave an untracked file in the project root, and a dirty project root
    silently blocks sprint auto-merge. It goes in the ``advisory/`` subdirectory
    because ``sprint.lock.sweep_story_locks`` globs ``.forge/locks/*.lock``
    non-recursively and deletes every lock nothing currently holds — these are
    not story locks and must not be swept as if they were.

    The name embeds the destination so the lock is greppable, plus a digest of
    the absolute path so two artifacts sharing a basename cannot share a lock.
    """
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    lock_dir = config.project_root / ".forge" / "locks" / "advisory"
    return lock_dir / f"{path.name}-{digest}.lock"


@contextmanager
def _artifact_lock(config: ForgeConfig, path: Path) -> Iterator[None]:
    """Hold an exclusive lock on *path*'s lock file for the duration of the block.

    ``fcntl.flock`` on a **separately-opened descriptor per acquisition** — the
    same primitive and shape ``sprint.lock`` uses — because sprint workers are
    threads in one process: POSIX record locks (``fcntl.lockf``) are per-process
    and would not serialize two worker threads at all, leaving the race intact
    behind a fix that looks like one. The acquisition is blocking: a contending
    writer must wait its turn, not skip its update.

    Raises:
        AdvisoryArtifactError: the lock could not be created or acquired.
    """
    try:
        lock_path = _lock_path(config, path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")  # noqa: SIM115
    except OSError as exc:
        raise AdvisoryArtifactError(path, exc) from exc
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError as exc:
            raise AdvisoryArtifactError(path, exc) from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - unlock failure on a held lock
            pass
        handle.close()


def _write_yaml_atomic(path: Path, data: dict[str, Any]) -> None:
    """Replace *path* with *data*, staged through a writer-unique scratch file.

    The scratch path must not be a deterministic function of the destination:
    every concurrent writer would derive the identical path, and one writer's
    ``replace()`` then either fails on a source another writer already renamed
    away (the observed ``FileNotFoundError``) or, worse, installs the other
    writer's content under this writer's name with no error at all.

    Raises:
        AdvisoryArtifactError: the artifact could not be written or replaced.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise AdvisoryArtifactError(path, exc) from exc
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)
        tmp_path.replace(path)
    except (OSError, yaml.YAMLError) as exc:
        _unlink_quietly(tmp_path)
        raise AdvisoryArtifactError(path, exc) from exc
    except BaseException:
        _unlink_quietly(tmp_path)
        raise


def _unlink_quietly(path: Path) -> None:
    """Remove this writer's own scratch file, never anyone else's."""
    try:
        path.unlink()
    except OSError:  # pragma: no cover - already gone or unremovable
        pass
