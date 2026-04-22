"""forge init / secrets-init / version subcommands."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from theforge.config import PROVIDER_API_KEY_MAP, generate_default_config

_GITIGNORE_ENTRY = ".forge/.env"

_STORY_TEMPLATE = """\
---
# Story frontmatter — required fields
name: "Short human-readable title"
slug: my-feature-slug        # used for branch and worktree names
---

# Story title

## Problem / context

One paragraph explaining WHY this change is needed and WHAT problem it solves.
Background and motivation belong here. This section is informational — it is
NOT a list of requirements.

## Acceptance criteria

<!--
  ACs are the definitive checklist. The dev agent will implement exactly what
  is stated here. Write them as observable, testable behaviors.
  Each AC should be a single bullet starting with a verb.
-->

- The system does X when Y
- Existing tests continue to pass
- New test verifies Z

## Notes (optional)

Any additional context, constraints, or design guidance. The dev agent will
read this as context, not as requirements.
"""


def _ensure_gitignored(project_root: Path) -> None:
    """Append .forge/.env to .gitignore if not already present."""
    gitignore = project_root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if _GITIGNORE_ENTRY in content.splitlines():
            return
        separator = "" if content.endswith("\n") else "\n"
        gitignore.write_text(content + separator + _GITIGNORE_ENTRY + "\n", encoding="utf-8")
    else:
        gitignore.write_text(_GITIGNORE_ENTRY + "\n", encoding="utf-8")


def _generate_secrets_skeleton() -> str:
    """Generate .env skeleton from PROVIDER_API_KEY_MAP."""
    lines = [
        "# .forge/.env — project-scoped secrets for TheForge",
        "# Copy this file to .forge/.env and fill in the values you need.",
        "# This file (.env.example) is tracked; .env is gitignored.",
        "",
    ]
    for _provider, key in PROVIDER_API_KEY_MAP.items():
        lines.append(f"# {key}=")
    lines.append("# NTFY_URL=https://ntfy.sh/your-topic-here")
    lines.append("")
    return "\n".join(lines)


def cmd_secrets_init(args: "argparse.Namespace") -> int:
    """Create .forge/.env skeleton and update .gitignore."""

    project_root = Path.cwd()
    env_path = project_root / ".forge" / ".env"
    secrets_yaml_path = project_root / ".forge" / "secrets.yaml"

    if secrets_yaml_path.exists() and not env_path.exists():
        print(
            "⚠ .forge/secrets.yaml detected — migrate to .forge/.env (see .forge/.env.example)",
            file=sys.stderr,
        )

    if env_path.exists():
        print(
            f"Warning: {env_path} already exists. Not overwriting.",
            file=sys.stderr,
        )
        return 0

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_generate_secrets_skeleton(), encoding="utf-8")
    print(f"Created {env_path}")

    _ensure_gitignored(project_root)
    print(f"Updated .gitignore to exclude {_GITIGNORE_ENTRY}")
    return 0


def cmd_init(args: "argparse.Namespace") -> int:
    """Generate a starter forge.yaml in the current directory."""

    target = Path.cwd() / "forge.yaml"
    if target.exists():
        print(f"forge.yaml already exists: {target}", file=sys.stderr)
        return 1

    target.write_text(generate_default_config(), encoding="utf-8")
    print(f"Created {target}")

    stories_dir = Path.cwd() / "stories"
    stories_dir.mkdir(exist_ok=True)
    template_path = stories_dir / "TEMPLATE.md"
    if not template_path.exists():
        template_path.write_text(_STORY_TEMPLATE, encoding="utf-8")
        print(f"Created {template_path}")

    print("Edit forge.yaml to match your project, then run: forge run <story-file>")

    _ensure_gitignored(Path.cwd())
    return 0


def _editable_source_path() -> str | None:
    """Return the filesystem path of the editable theforge checkout, or None.

    Reads direct_url.json (present only for editable/VCS installs) and
    converts the ``file://`` URL to an absolute path.  Returns None for
    PyPI installs or when the metadata is unavailable.
    """
    try:
        dist = importlib.metadata.distribution("theforge")
    except importlib.metadata.PackageNotFoundError:
        return None

    text = dist.read_text("direct_url.json")
    if not text:
        return None

    try:
        data = json.loads(text)
        url: str = data.get("url", "")
    except (json.JSONDecodeError, AttributeError):
        return None

    if not url.startswith("file://"):
        return None
    # file:///abs/path  →  /abs/path
    return url[len("file://") :]


def _get_dev_suffix(source_path: str | None = None) -> str:
    """Return '-dev+g<hash>' when editable install is ahead of the latest tag.

    Pass *source_path* (the editable checkout directory) to avoid a second
    call to ``_editable_source_path``.  Returns an empty string when:
    - not an editable install (no direct_url.json)
    - at exact tag (distance == 0)
    - git is unavailable
    """
    if source_path is None:
        source_path = _editable_source_path()
    if source_path is None:
        return ""

    try:
        # Output format: <tag>-<distance>-g<hash>  e.g. v0.2.1-5-g8704ff0
        describe = subprocess.check_output(
            ["git", "describe", "--tags", "--long"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=source_path,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""

    parts = describe.rsplit("-", 2)
    if len(parts) != 3:
        return ""
    _, distance_str, short_hash = parts
    try:
        distance = int(distance_str)
    except ValueError:
        return ""

    if distance == 0:
        return ""
    return f"-dev+{short_hash}"


def cmd_version(args: "argparse.Namespace") -> int:
    """Print the installed version of TheForge."""

    import theforge

    source_path = _editable_source_path()
    version = theforge.__version__ + _get_dev_suffix(source_path)
    print(f"TheForge version: {version}")

    if source_path is not None:
        # Try to get git info from the editable checkout
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                cwd=source_path,
            ).strip()
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                cwd=source_path,
            ).strip()
            tag_distance = subprocess.check_output(
                ["git", "describe", "--tags", "--long"],
                text=True,
                stderr=subprocess.DEVNULL,
                cwd=source_path,
            ).strip()
            print(f"  Branch: {branch}")
            print(f"  Commit: {commit}")
            print(f"  Tag distance: {tag_distance}")
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            print("  (Git information not available)")

    return 0
