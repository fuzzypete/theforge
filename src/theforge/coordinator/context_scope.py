from __future__ import annotations


def plan_file_list(plan_structured: dict | None) -> list[str]:
    """Extract a deduped ordered file list from structured plan steps."""
    files: list[str] = []
    seen: set[str] = set()
    for step in (plan_structured or {}).get("steps", []):
        for path in step.get("files", []) or []:
            if not isinstance(path, str) or not path or path in seen:
                continue
            seen.add(path)
            files.append(path)
    return files
