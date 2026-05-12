"""Rolling aggregation and surfacing for advisory convention violations."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
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
    """Update the rolling artifact from the current advisory convention scan."""
    advisory_cfg = config.conventions_advisory
    observed_entries: dict[str, dict[str, Any]] = {}
    existing = load_advisory_summary(config)
    existing_entries = existing.get("entries", {})
    if not isinstance(existing_entries, dict):
        existing_entries = {}

    observed_at_str = observed_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    newly_filed: list[dict[str, Any]] = []

    for violation in violations:
        normalized = _normalize_advisory_violation(violation)
        if normalized is None:
            continue
        key = _entry_key(normalized["rule"], normalized["file"])
        prior = existing_entries.get(key) if isinstance(existing_entries.get(key), dict) else None
        entry = {
            "rule": normalized["rule"],
            "file": normalized["file"],
            "detail": normalized["detail"],
            "line_count": normalized["line_count"],
            "limit": normalized["limit"],
            "gap": normalized["gap"],
            "first_seen": prior.get("first_seen") if prior else observed_at_str,
            "last_seen": observed_at_str,
            "last_run_id": run_id,
            "last_story_slug": story_slug,
        }
        issue_block = (
            dict(prior.get("issue", {})) if prior and isinstance(prior.get("issue"), dict) else {}
        )
        if issue_block:
            entry["issue"] = issue_block
        observed_entries[key] = entry

    for key, entry in observed_entries.items():
        maybe_issue = _maybe_file_issue(config, entry)
        if maybe_issue is not None:
            entry["issue"] = maybe_issue
            newly_filed.append({"key": key, **maybe_issue})

    artifact_data = {
        "updated_at": observed_at_str,
        "entries": observed_entries,
    }
    _write_yaml_atomic(advisory_artifact_path(config), artifact_data)
    if advisory_cfg.commit_shared_artifact and advisory_cfg.shared_artifact_path:
        _write_yaml_atomic(config.project_root / advisory_cfg.shared_artifact_path, artifact_data)

    return {
        "path": str(advisory_artifact_path(config)),
        "entry_count": len(observed_entries),
        "newly_filed_issues": newly_filed,
        "entries": observed_entries,
    }


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


def _write_yaml_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)
    tmp_path.replace(path)
