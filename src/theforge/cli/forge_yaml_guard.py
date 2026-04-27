"""Mechanical guard for story-branch mutations to forge.yaml."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from theforge.cli.shared import _find_config
from theforge.config import load_config
from theforge.task import (
    ALLOW_MUTATE_FORGE_YAML_KEY,
    frontmatter_allows_forge_yaml_mutation,
    parse_story_frontmatter,
)

_ALLOWED_TOP_LEVEL_KEYS = frozenset({"assignment", "models"})


@dataclass(frozen=True)
class ForgeYamlGuardResult:
    """Outcome of the forge.yaml story-mutation guard."""

    ok: bool
    violating_keys: tuple[str, ...] = ()
    override_active: bool = False
    changed_keys: tuple[str, ...] = ()


def _load_yaml_mapping(raw: str, *, source: str) -> dict[str, Any]:
    """Parse YAML text as a mapping or raise ValueError."""
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{source} must parse to a top-level mapping")
    return data


def _changed_top_level_keys(base: dict[str, Any], current: dict[str, Any]) -> tuple[str, ...]:
    """Return sorted top-level keys whose values differ between two forge.yaml mappings."""
    keys = set(base) | set(current)
    changed = [key for key in keys if base.get(key) != current.get(key)]
    return tuple(sorted(changed))


def _current_branch(repo_root: Path) -> str:
    """Return the current branch name."""
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "git rev-parse failed"
        raise RuntimeError(err)
    return proc.stdout.strip()


def _issue_number_from_branch(branch: str) -> int | None:
    """Extract an issue number from a story branch name like feat/issue-283."""
    for part in branch.split("/"):
        if part.startswith("issue-") and part[6:].isdigit():
            return int(part[6:])
    return None


def _branch_slug(branch: str) -> str:
    """Return the branch leaf used for slug or filename matching."""
    return branch.rsplit("/", 1)[-1]


def _iter_local_story_paths(repo_root: Path) -> list[Path]:
    """Return candidate local story files under the repository root."""
    candidates: list[Path] = []
    for path in repo_root.rglob("*.md"):
        relative_parts = path.relative_to(repo_root).parts
        if any(part.startswith(".") for part in relative_parts):
            continue
        candidates.append(path)
    return sorted(candidates)


def _matches_story_issue(frontmatter: dict[str, Any], issue_number: int) -> bool:
    """Return whether story frontmatter references the current issue."""
    raw_issue = frontmatter.get("github_issue")
    try:
        return int(raw_issue) == issue_number
    except (TypeError, ValueError):
        return False


def _local_story_override_active(repo_root: Path, branch: str) -> bool:
    """Return whether a matching local story file opts into forge.yaml mutations."""
    issue_number = _issue_number_from_branch(branch)
    slug = _branch_slug(branch)

    for story_path in _iter_local_story_paths(repo_root):
        frontmatter = parse_story_frontmatter(story_path)
        if not frontmatter:
            continue

        if issue_number is not None and _matches_story_issue(frontmatter, issue_number):
            return frontmatter_allows_forge_yaml_mutation(frontmatter)

        story_slug = frontmatter.get("slug")
        if story_slug == slug or story_path.stem == slug:
            return frontmatter_allows_forge_yaml_mutation(frontmatter)

    return False


def _load_issue_metadata(repo_root: Path, issue_number: int) -> dict[str, Any]:
    """Best-effort fetch of issue body metadata for explicit guard overrides."""
    proc = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "body"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    body = data.get("body", "")
    if not isinstance(body, str) or not body.startswith("---"):
        return {}
    end = body.find("---", 3)
    if end == -1:
        return {}
    try:
        metadata = yaml.safe_load(body[3:end].strip()) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    return metadata


def _issue_override_active(repo_root: Path) -> bool:
    """Return whether the current story branch explicitly allows forge.yaml mutations."""
    try:
        branch = _current_branch(repo_root)
    except RuntimeError:
        return False
    if _local_story_override_active(repo_root, branch):
        return True
    issue_number = _issue_number_from_branch(branch)
    if issue_number is None:
        return False
    metadata = _load_issue_metadata(repo_root, issue_number)
    return metadata.get(ALLOW_MUTATE_FORGE_YAML_KEY) is True


def evaluate_forge_yaml_guard(repo_root: Path, *, base_branch: str) -> ForgeYamlGuardResult:
    """Evaluate the forge.yaml mutation guard for the current branch."""
    current_path = repo_root / "forge.yaml"
    if not current_path.exists():
        return ForgeYamlGuardResult(ok=True)

    diff_proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base_branch}...HEAD", "--", "forge.yaml"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=30,
        check=False,
    )
    if diff_proc.returncode != 0:
        err = diff_proc.stderr.strip() or diff_proc.stdout.strip() or "git diff failed"
        raise RuntimeError(err)
    if not diff_proc.stdout.strip():
        return ForgeYamlGuardResult(ok=True)

    current = _load_yaml_mapping(current_path.read_text(encoding="utf-8"), source="forge.yaml")
    base_proc = subprocess.run(
        ["git", "show", f"{base_branch}:forge.yaml"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=30,
        check=False,
    )
    if base_proc.returncode != 0:
        err = base_proc.stderr.strip() or base_proc.stdout.strip() or "git show failed"
        raise RuntimeError(err)
    base = _load_yaml_mapping(base_proc.stdout, source=f"{base_branch}:forge.yaml")

    changed_keys = _changed_top_level_keys(base, current)
    if not changed_keys:
        return ForgeYamlGuardResult(ok=True)

    override_active = _issue_override_active(repo_root)
    violating_keys = tuple(key for key in changed_keys if key not in _ALLOWED_TOP_LEVEL_KEYS)
    if override_active or not violating_keys:
        return ForgeYamlGuardResult(
            ok=True,
            violating_keys=violating_keys,
            override_active=override_active,
            changed_keys=changed_keys,
        )
    return ForgeYamlGuardResult(
        ok=False,
        violating_keys=violating_keys,
        override_active=False,
        changed_keys=changed_keys,
    )


def register_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the guard subcommand."""
    parser = subparsers.add_parser(
        "check-story-config",
        help="Reject story-branch forge.yaml edits outside the mutable allowlist",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to forge.yaml (defaults to nearest forge.yaml from cwd)",
    )


def cmd_check_story_config(args: argparse.Namespace) -> int:
    """Run the story-branch forge.yaml mutation guard."""
    config_path = Path(args.config).resolve() if args.config else _find_config()
    if config_path is None:
        print("forge.yaml not found", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
        result = evaluate_forge_yaml_guard(
            config.project_root,
            base_branch=config.workspace.base_branch,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"forge.yaml story-mutation guard failed: {exc}", file=sys.stderr)
        return 1

    if result.ok:
        return 0

    allowed = ", ".join(sorted(_ALLOWED_TOP_LEVEL_KEYS))
    violating = ", ".join(result.violating_keys)
    print(
        "forge.yaml story-mutation guard failed: "
        f"diff touches non-mutable keys: {violating}. "
        f"Allowed story-mutable top-level keys: {allowed}. "
        f"To override for this issue, add `{ALLOW_MUTATE_FORGE_YAML_KEY}: true` "
        "to the issue's leading YAML metadata block.",
        file=sys.stderr,
    )
    return 1
