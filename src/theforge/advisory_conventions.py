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


def _maybe_file_issue(config: ForgeConfig, entry: dict[str, Any]) -> dict[str, Any] | None:
    issue_cfg = config.conventions_advisory.issue_filing
    if not issue_cfg.enabled:
        return entry.get("issue") if isinstance(entry.get("issue"), dict) else None
    if isinstance(entry.get("issue"), dict):
        return entry["issue"]
    percent_over = _entry_percent_over(entry)
    if percent_over is None or percent_over < issue_cfg.threshold_percent:
        return None

    title = f"Advisory convention debt: {entry['rule']} in {entry['file']}"
    body_lines = [
        "## Advisory convention violation",
        "",
        f"- Rule: `{entry['rule']}`",
        f"- File: `{entry['file']}`",
        f"- Detail: {entry['detail']}",
        f"- Last observed run: `{entry.get('last_run_id') or 'unknown'}`",
    ]
    if entry.get("last_story_slug"):
        body_lines.append(f"- Observed while running: `{entry['last_story_slug']}`")
    body_lines.extend(
        [
            "",
            "## Audit excerpt",
            "",
            "```yaml",
            yaml.safe_dump(
                {
                    "rule": entry["rule"],
                    "file": entry["file"],
                    "detail": entry["detail"],
                    "blocking": False,
                },
                sort_keys=False,
            ).strip(),
            "```",
        ]
    )
    command = [
        "gh",
        "issue",
        "create",
        "--title",
        title,
        "--body",
        "\n".join(body_lines),
        "--label",
        issue_cfg.label,
    ]
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
        "milestone": issue_cfg.milestone,
        "filed_at": entry["last_seen"],
    }


def _write_yaml_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)
    tmp_path.replace(path)
